import datetime
import json
import os
import sys
import traceback
import time

import netaddr
import oslo_messaging

from com.vmware.nsx_client import TransportZones
from com.vmware.nsx.model_client import (FirewallRule,
                                         IPSet,
                                         LogicalPort,
                                         QosSwitchingProfile,
                                         TransportZone)
from neutron.common import config as common_config
from neutron.common import profiler, topics
from neutron.plugins.ml2.drivers.agent import _agent_manager_base as amb
from neutron.plugins.ml2.drivers.agent import _common_agent as ca
from neutron_lib.api.definitions import portbindings
from neutron_lib import context as neutron_context
from oslo_config import cfg
from oslo_log import log as logging
from oslo_service import service

from networking_nsxv3.common import config  # noqa
from networking_nsxv3.common import constants as nsxv3_constants
from networking_nsxv3.common.locking import LockManager
from networking_nsxv3.api import rpc as nsxv3_rpc
from networking_nsxv3.plugins.ml2.drivers.nsxv3.agent import nsxv3_facada
from networking_nsxv3.plugins.ml2.drivers.nsxv3.agent import nsxv3_utils
from networking_nsxv3.plugins.ml2.drivers.nsxv3.agent import vsphere_client
from networking_nsxv3.common import synchronization as sync

# Eventlet Best Practices
# https://specs.openstack.org/openstack/openstack-specs/specs/eventlet-best-practices.html
if not os.environ.get('DISABLE_EVENTLET_PATCHING'):
    import eventlet
    eventlet.monkey_patch()

LOG = logging.getLogger(__name__)

AGENT_SYNCHRONIZATION_LOCK = "AGENT_SYNCHRONIZATION_LOCK"


def is_migration_enabled():
    return cfg.CONF.AGENT.enable_runtime_migration_from_dvs_driver


class NSXv3AgentManagerRpcCallBackBase(amb.CommonAgentManagerRpcCallBackBase):

    target = oslo_messaging.Target(version=nsxv3_constants.RPC_VERSION)

    """
    Base class for managers RPC callbacks.
    """

    def __init__(self, context, agent, sg_agent, nsxv3, vsphere, rpc):
        super(NSXv3AgentManagerRpcCallBackBase, self).__init__(
            context, agent, sg_agent)
        self.nsxv3 = nsxv3
        self.vsphere = vsphere
        self.rpc = rpc
        self.runner = sync.Runner(
            workers_size=cfg.CONF.NSXV3.nsxv3_concurrent_requests)
        self.runner.start()

    def security_group_updated(self, security_group_id):
        sg_id = str(security_group_id)
        LOG.debug("Updating Security Group '{}'".format(sg_id))
        with LockManager.get_lock(sg_id):
            self.nsxv3.get_or_create_security_group(sg_id)

            tcp_strict_enabled = self.rpc.has_security_group_tag(
                security_group_id, nsxv3_constants.NSXV3_CAPABILITY_TCP_STRICT)
            self.nsxv3.update_security_group_capabilities(sg_id,
                                                          [tcp_strict_enabled])

    def security_group_member_updated(self, security_group_id):
        sg_id = str(security_group_id)
        LOG.debug("Updating Security Group '{}' members".format(sg_id))
        with LockManager.get_lock(sg_id):
            self.nsxv3.get_or_create_security_group(sg_id)
            ip1 = self.rpc.get_security_group_members_ips(sg_id)
            ip2 = self.rpc.get_security_group_members_address_bindings_ips(
                sg_id)

            members = [str(ip) for ip in netaddr.IPSet(
                [str(ip[0]) for ip in ip1 + ip2]).iter_cidrs()]
            self.nsxv3.update_security_group_members(sg_id, members)

    def security_group_rule_updated(self, security_group_id):
        sg_id = str(security_group_id)
        LOG.debug("Updating Security Group '{}' rules".format(sg_id))
        with LockManager.get_lock(sg_id):
            (ipset, nsg, sec) = self.nsxv3.get_or_create_security_group(sg_id)
            _, revs_ips, _ = self.nsxv3.get_revisions(IPSet())
            _, revs_fwr, meta_fwr = self.nsxv3.get_revisions(
                FirewallRule(),
                attr_key="section_id",
                attr_val=sec.id)

            neutron_rules = self.rpc.get_rules_for_security_groups_id(sg_id)

            attrs = ["id", "port_range_min", "port_range_max", "protocol",
                     "ethertype", "direction", "remote_group_id",
                     "remote_ip_prefix", "security_group_id"]

            add_rules = []
            for rule in neutron_rules:
                name = rule["id"]
                remote_group_id = rule["remote_group_id"]

                fwr = dict()
                for key in attrs:
                    fwr[key] = rule[key]

                fwr["local_group_id"] = ipset.id
                fwr["apply_to"] = nsg.id
                if remote_group_id in revs_ips:
                    fwr["remote_group_id"] = revs_ips[remote_group_id]

                if name in revs_fwr:
                    # If the rule is disabled recreate it
                    if not meta_fwr.get(name).get("FirewallRule.disabled"):
                        del revs_fwr[name]
                        continue

                fwr_spec = self.nsxv3.get_security_group_rule_spec(fwr)
                if fwr_spec:
                    add_rules.append(fwr_spec)
            del_rules = revs_fwr.values()

            (sg_id, revision) = self.rpc.get_security_group_revision(sg_id)

            self.nsxv3.update_security_group_rules(
                sg_id,
                revision_number=revision,
                add_rules=add_rules,
                del_rules=del_rules)

    def security_group_delete(self, security_group_id):
        with LockManager.get_lock(security_group_id):
            self.nsxv3.delete_security_group(security_group_id)

    # RPC method
    def security_groups_member_updated(self, context, **kwargs):
        o = kwargs["security_groups"]
        self.runner.run(sync.Priority.HIGHEST,
                        o if isinstance(o, list) else [o],
                        self.security_group_member_updated)

    # RPC method
    def security_groups_rule_updated(self, context, **kwargs):
        o = kwargs["security_groups"]
        self.runner.run(sync.Priority.HIGHEST,
                        o if isinstance(o, list) else [o],
                        self.security_group_rule_updated)

    def _sync_inventory_full(self):
        self.runner.run(
            sync.Priority.HIGHER,
            self.get_revisions(
                query=self.rpc.get_security_group_revision_tuples).keys(),
            self.sync_security_group)
        self.runner.run(
            sync.Priority.HIGH,
            self.get_revisions(
                query=self.rpc.get_qos_policy_revision_tuples).keys(),
            self.sync_qos)
        self.runner.run(sync.Priority.MEDIUM, self.get_revisions(
            query=self.rpc.get_port_revision_tuples).keys(), self.sync_port)

    def _sync_inventory_shallow(self):
        sg_query = self.rpc.get_security_group_revision_tuples
        qos_query = self.rpc.get_qos_policy_revision_tuples
        port_query = self.rpc.get_port_revision_tuples

        # Security Groups Synchronization
        outdated_ips, orphaned_ips = self._sync_get_content(
            sdk_model=IPSet(), os_query=sg_query)
        self.runner.run(
            sync.Priority.HIGHER,
            outdated_ips, self.security_group_updated)
        # Create all Security Groups before use their references in rules
        # Creating FirewallSections, IPSets, NSGroups without FirewallRules
        self.runner.run(
            sync.Priority.HIGH,
            outdated_ips, self.security_group_member_updated)
        self.runner.run(
            sync.Priority.MEDIUM,
            outdated_ips, self.security_group_rule_updated)
        self.runner.run(
            sync.Priority.LOW,
            orphaned_ips, self.sync_security_group_orphaned)

        # QoS Policies Synchronization
        outdated_qos, orphaned_qos = self._sync_get_content(
            sdk_model=QosSwitchingProfile(), os_query=qos_query)
        self.runner.run(
            sync.Priority.LOWER,
            outdated_qos, self.sync_qos)

        # Ports Synchronization
        outdated_lps, orphaned_lps = self._sync_get_content(
            sdk_model=LogicalPort(), os_query=port_query)
        self.runner.run(
            sync.Priority.LOW,
            outdated_lps, self.sync_port)
        self.runner.run(
            sync.Priority.LOW,
            orphaned_lps, self.sync_port_orphaned)

        self._sync_report("Security Groups", outdated_ips, orphaned_ips)
        self._sync_report("QoS Profiles", outdated_qos, orphaned_qos)
        self._sync_report("Ports", outdated_lps, orphaned_lps)

    def sync_inventory(self):
        m = "Synchronization events pools size HIGHPRIORITY={} LOWPRIORITY={}"

        with LockManager.get_lock(AGENT_SYNCHRONIZATION_LOCK):
            LOG.info(m.format(self.runner.active(), self.runner.passive()))

            if self.runner.passive() > 0:
                return

            timestamp = nsxv3_facada.Timestamp(
                "last_full_synchronization",
                self.nsxv3, TransportZones,
                TransportZone(display_name=self.nsxv3.tz_name),
                cfg.CONF.AGENT.sync_full_schedule)

            if timestamp.has_expired():
                LOG.info("Starting a full inventory synchronization")
                self._sync_inventory_full()
                timestamp.update()
            else:
                LOG.info("Starting a shallow inventory synchronization")
                self._sync_inventory_shallow()

    def _sync_report(self, object_name, outdated, orphaned):
        report = dict()
        report["outdated"] = outdated
        report["orphaned"] = orphaned
        LOG.info("Synchronizing {} {}".format(object_name, report))

    def _sync_get_content(self, sdk_model, os_query):
        revs_os = self.get_revisions(query=os_query)
        revs_nsx, _, _ = self.nsxv3.get_revisions(sdk_model=sdk_model)

        outdated = set()

        for key, rev in revs_os.items():
            if revs_nsx.get(key) != rev:
                outdated.add(key)

        orphaned = set(revs_nsx.keys()).difference(revs_os.keys())
        return outdated, orphaned

    def sync_port(self, port_id):
        LOG.debug("Synching port '{}'.".format(port_id))

        (id, mac, up, status, qos_id, rev,
         binding_host, vif_details) = self.rpc.get_port(port_id)
        port = {
            "id": id,
            "mac_address": mac,
            "admin_state_up": up,
            "status": status,
            "qos_policy_id": qos_id,
            "fixed_ips": [],
            "allowed_address_pairs": [],
            "security_groups": [],
            "revision_number": rev,
            "binding:host_id": binding_host,
            portbindings.VNIC_TYPE: portbindings.VNIC_NORMAL,
            portbindings.VIF_TYPE: portbindings.VIF_TYPE_OVS
        }

        segmentation_id = json.loads(vif_details).get("segmentation_id")

        for ip, subnet in self.rpc.get_port_addresses(port_id):
            port["fixed_ips"].append(
                {"ip_address": ip, "mac_address": mac, "subnet_id": subnet})

        for (ip, mac) in self.rpc.get_port_allowed_pairs(port_id):
            # TODO - fix in future.
            # NSX-T does not support CIDR as port manual binding
            if "/" in ip:
                continue
            port["allowed_address_pairs"].append(
                {"ip_address": ip, "mac_address": mac})

        for (sg_id,) in self.rpc.get_port_security_groups(port_id):
            port["security_groups"].append(sg_id)

        self.port_update(context=None, port=port,
                         segmentation_id=segmentation_id)

    def sync_port_orphaned(self, port_id):
        LOG.debug("Removing orphaned ports '{}'.".format(
            port_id))
        self.port_delete(context=None, port_id=port_id, sync=True)

    def sync_qos(self, qos_id):
        LOG.debug("Synching QoS porofile '{}'.".format(qos_id))
        (qos_name, qos_revision_number) = self.rpc.get_qos(qos_id)
        bwls_rules = self.rpc.get_qos_bwl_rules(qos_id)
        dscp_rules = self.rpc.get_qos_dscp_rules(qos_id)

        rules = []
        if dscp_rules:
            for (_, dscp_mark) in dscp_rules:
                rules.append({"dscp_mark": dscp_mark})
        if bwls_rules:
            for (direction, max_kbps, max_burst_kbps) in bwls_rules:
                rules.append({
                    "direction": direction,
                    "max_kbps": max_kbps,
                    "max_burst_kbps": max_burst_kbps
                })

        policy = {
            "id": qos_id,
            "name": qos_name,
            "revision_number": qos_revision_number,
            "rules": rules
        }
        try:
            self.create_policy(context=None, policy=policy)
        except Exception as e:
            if "Object exists" not in str(e):
                LOG.error("Unable to create policy '{}'".format(qos_id))
        # try:
        self.update_policy(context=None, policy=policy)
        # except Exception as e:
        #     LOG.error("Unable to update policy '{}'".format(e))

    def sync_qos_orphaned(self, qos_id):
        LOG.debug("Removing orphaned QoS Policy '{}'.".format(qos_id))
        policy = {
            "id": qos_id,
            "name": "ORPHANED-" + qos_id
        }
        self.delete_policy(context=None, policy=policy)

    def sync_security_group(self, security_group_id, update_rules=True):
        LOG.debug("Synching Security Group '{}'.".format(security_group_id))
        self.security_group_updated(security_group_id)
        self.security_group_member_updated(security_group_id)
        if update_rules:
            self.security_group_rule_updated(security_group_id)

    def sync_security_group_orphaned(self, security_group_id):
        LOG.debug("Removing orphaned security group '{}'.".format(
            security_group_id))
        self.security_group_delete(security_group_id)

    def get_revisions(self, query):
        limit = cfg.CONF.AGENT.rpc_max_records_per_query
        rev = {}
        created_after = datetime.datetime(1970, 1, 1)
        while True:
            pr_tuples = query(limit=limit, created_after=created_after)
            for port, revision, _ in pr_tuples:
                rev[port] = str(revision)
            if len(pr_tuples) < limit:
                break
            created_after = pr_tuples.pop()[2]
        return rev

    def get_network_bridge(
            self,
            context,
            current,
            network_segments,
            network_current):
        LOG.debug("Trying to map network bridge for networks ...")
        for ns in network_segments:
            seg_id = ns.get("segmentation_id")
            if seg_id:
                LOG.debug("Retrieving bridge for segmentation_id={}"
                          .format(seg_id))
                lock_id = nsxv3_utils.get_segmentation_id_lock(seg_id)
                with LockManager.get_lock(lock_id):
                    id = self.nsxv3.get_switch_id_for_segmentation_id(seg_id)
                    return {
                        'nsx-logical-switch-id': id,
                        'segmentation_id': seg_id
                    }
        return {}

    def port_update(self, context, port=None, network_type=None,
                    physical_network=None, segmentation_id=None):
        vnic_type = port.get(portbindings.VNIC_TYPE)
        vif_type = port.get(portbindings.VIF_TYPE)
        if not ((vnic_type and vnic_type == portbindings.VNIC_NORMAL) and
                (vif_type and vif_type == portbindings.VIF_TYPE_OVS)):
            return

        if 'binding:host_id' in port:
            if not cfg.CONF.host == port.get('binding:host_id'):
                LOG.debug("Skipping Port='%s'. It is not assigned to agent.",
                          str(port))
                return

        LOG.debug("Updating Port='%s' with Segment='%s'", str(port),
                  segmentation_id)

        address_bindings = []

        for addr in port["fixed_ips"]:
            mac = addr.get("mac_address")
            mac = mac if mac else port["mac_address"]
            address_bindings.append((addr["ip_address"], mac))
        for addr in port["allowed_address_pairs"]:
            address_bindings.append((addr["ip_address"], addr["mac_address"]))

        with LockManager.get_lock(port["id"]):
            self.nsxv3.port_update(
                port["id"],
                port["revision_number"],
                port["security_groups"],
                address_bindings,
                qos_name=port.get("qos_policy_id")
            )
        self.updated_devices.add(port['mac_address'])

    def port_delete(self, context, **kwargs):
        LOG.debug("Deleting port " + str(kwargs))
        if kwargs.get("sync"):
            with LockManager.get_lock(kwargs["port_id"]):
                self.nsxv3.port_delete(kwargs["port_id"])
        # Else, a port is deleted by Nova when destroying the instance

    def create_policy(self, context, policy):
        LOG.debug("Creating policy={}.".format(policy["name"]))
        with LockManager.get_lock(policy["id"]):
            self.nsxv3.create_switch_profile_qos(
                policy["id"], policy["revision_number"])

    def update_policy(self, context, policy):
        LOG.debug("Updating policy={}.".format(policy["name"]))
        with LockManager.get_lock(policy["id"]):
            self.nsxv3.update_switch_profile_qos(context, policy["id"],
                                                 policy["revision_number"],
                                                 policy["rules"])

    def delete_policy(self, context, policy):
        LOG.debug("Deleting policy={}.".format(policy["name"]))
        with LockManager.get_lock(policy["id"]):
            pass
            # TODO self.nsxv3.delete_switch_profile_qos(policy["id"])

    def validate_policy(self, context, policy):
        LOG.debug("Validating policy={}.".format(policy["name"]))
        self.nsxv3.validate_switch_profile_qos(policy["rules"])


class NSXv3Manager(amb.CommonAgentManagerBase):

    def __init__(self, nsxv3=None, vsphere=None):
        super(NSXv3Manager, self).__init__()
        context = neutron_context.get_admin_context()

        self.nsxv3 = nsxv3
        self.vsphere = vsphere
        self.rpc = None
        self.rpc_plugin = nsxv3_rpc.NSXv3ServerRpcApi(
            context, nsxv3_constants.NSXV3_SERVER_RPC_TOPIC, cfg.CONF.host)
        self.last_sync_time = 0

    def get_all_devices(self):
        """Get a list of all devices of the managed type from this host
        A device in this context is a String that represents a network device.
        This can for example be the name of the device or its MAC address.
        This value will be stored in the Plug-in and be part of the
        device_details.
        Typically this list is retrieved from the sysfs. E.g. for linuxbridge
        it returns all names of devices of type 'tap' that start with a certain
        prefix.
        :return: set -- the set of all devices e.g. ['tap1', 'tap2']
        """

        if self.rpc:
            try:
                # get_all_devices is called by sync loop and report which
                # have different frequency. Here it is ensured that sync loop
                # will not be called more often than the sync loop
                now = time.time()
                elapsed = (time.time() - self.last_sync_time)
                if elapsed > cfg.CONF.AGENT.polling_interval:
                    self.rpc.sync_inventory()
                    self.last_sync_time = now
            except Exception:
                LOG.error(traceback.format_exc())
        return set()

    def get_devices_modified_timestamps(self, devices):
        """Get a dictionary of modified timestamps by device
        The devices passed in are expected to be the same format that
        get_all_devices returns.
        :return: dict -- A dictionary of timestamps keyed by device
        """
        return dict()

    def plug_interface(
            self,
            network_id,
            network_segment,
            device,
            device_owner):
        # NSXv3 Agent does not plug standard ports it self, it relies on Nova
        pass

    def ensure_port_admin_state(self, device, admin_state_up):
        """Enforce admin_state for a port
        :param device: The device for which the admin_state should be set
        :param admin_state_up: True for admin_state_up, False for
            admin_state_down
        """

    def get_agent_configurations(self):
        """Establishes the agent configuration map.
        The content of this map is part of the agent state reports to the
        neutron server.
        :return: map -- the map containing the configuration values
        :rtype: dict
        """
        c = cfg.CONF.NSXV3
        return {
            'nsxv3_connection_retry_count': c.nsxv3_connection_retry_count,
            'nsxv3_connection_retry_sleep': c.nsxv3_connection_retry_sleep,
            'nsxv3_request_timeout': c.nsxv3_request_timeout,
            'nsxv3_host': c.nsxv3_login_hostname,
            'nsxv3_port': c.nsxv3_login_port,
            'nsxv3_user': c.nsxv3_login_user,
            'nsxv3_password': c.nsxv3_login_password,
            'nsxv3_managed_hosts': c.nsxv3_managed_hosts,
            'nsxv3_transport_zone': c.nsxv3_transport_zone_name}

    def get_agent_id(self):
        """Calculate the agent id that should be used on this host
        :return: str -- agent identifier
        """
        return cfg.CONF.AGENT.agent_id

    def get_extension_driver_type(self):
        """Get the agent extension driver type.
        :return: str -- The String defining the agent extension type
        """
        return nsxv3_constants.NSXV3

    def get_rpc_callbacks(self, context, agent, sg_agent):
        """Returns the class containing all the agent rpc callback methods
        :return: class - the class containing the agent rpc callback methods.
            It must reflect the CommonAgentManagerRpcCallBackBase Interface.
        """
        if not self.rpc:
            self.rpc = NSXv3AgentManagerRpcCallBackBase(
                context=context,
                agent=agent,
                sg_agent=sg_agent,
                nsxv3=self.nsxv3,
                vsphere=self.vsphere,
                rpc=self.rpc_plugin)
        return self.rpc

    def get_agent_api(self, **kwargs):
        """Get L2 extensions drivers API interface class.
        :return: instance of the class containing Agent Extension API
        """

    def get_rpc_consumers(self):
        """Get a list of topics for which an RPC consumer should be created
        :return: list -- A list of topics. Each topic in this list is a list
            consisting of a name, an operation, and an optional host param
            keying the subscription to topic.host for plugin calls.
        """
        return [
            [topics.PORT, topics.UPDATE],
            [topics.PORT, topics.DELETE],
            [topics.SECURITY_GROUP, topics.UPDATE],
            [nsxv3_constants.NSXV3, topics.UPDATE]
        ]

    def setup_arp_spoofing_protection(self, device, device_details):
        """Setup the arp spoofing protection for the given port.
        :param device: The device to set up arp spoofing rules for, where
            device is the device String that is stored in the Neutron Plug-in
            for this Port. E.g. 'tap1'
        :param device_details: The device_details map retrieved from the
            Neutron Plugin
        """
        # Spoofguard is handled by port update operation

    def delete_arp_spoofing_protection(self, devices):
        """Remove the arp spoofing protection for the given ports.
        :param devices: List of devices that have been removed, where device
            is the device String that is stored for this port in the Neutron
            Plug-in. E.g. ['tap1', 'tap2']
        """
        # Spoofguard is handled by port delete operation

    def delete_unreferenced_arp_protection(self, current_devices):
        """Cleanup arp spoofing protection entries.
        :param current_devices: List of devices that currently exist on this
            host, where device is the device String that could have been stored
            in the Neutron Plug-in. E.g. ['tap1', 'tap2']
        """


def cli_sync():
    """
    CLI SYNC command force synchronization between Neutron and NSX-T objects
    cfg.CONF.AGENT_CLI for options
    """
    LOG.info("VMware NSXv3 Agent CLI")
    common_config.init(sys.argv[1:])
    common_config.setup_logging()
    profiler.setup(nsxv3_constants.NSXV3_BIN, cfg.CONF.host)

    sg_ids = cfg.CONF.AGENT_CLI.neutron_security_group_id
    pt_ids = cfg.CONF.AGENT_CLI.neutron_port_id
    qs_ids = cfg.CONF.AGENT_CLI.neutron_qos_policy_id

    nsxv3 = nsxv3_facada.NSXv3Facada()
    # Force login as NSXv3Manager will not be started as daemon.
    nsxv3.login()
    manager = NSXv3Manager(nsxv3=nsxv3)
    rpc = manager.get_rpc_callbacks(context=None, agent=None, sg_agent=None)

    def execute(callback, ids):
        status = {}
        error = False
        for id in ids:
            try:
                callback(id)
                status[id] = "Success"
            except Exception as e:
                error = True
                status[id] = "Error: {}".format(str(e))
                LOG.exception(e)
        return (status, error)

    (pt_status, pt_error) = execute(rpc.sync_port, pt_ids)
    (sg_status, sg_error) = execute(rpc.sync_security_group, sg_ids)
    (qs_status, qs_error) = execute(rpc.sync_qos, qs_ids)

    result = {
        "security_groups": sg_status,
        "ports": pt_status,
        "qos_policies": qs_status
    }

    LOG.info(json.dumps(result))

    return 1 if pt_error or sg_error or qs_error else 0


def main():
    LOG.info("VMware NSXv3 Agent initializing ...")
    common_config.init(sys.argv[1:])
    common_config.setup_logging()
    profiler.setup(nsxv3_constants.NSXV3_BIN, cfg.CONF.host)

    # Enable DEBUG Logging
    try:
        resolution = float(os.getenv('DEBUG_BLOCKING'))
        eventlet.debug.hub_blocking_detection(
            state=True, resolution=resolution)
    except (ValueError, TypeError):
        LOG.error("VMware NSXv3 Agent setting DEBUG configuration has failed.")

    nsxv3 = nsxv3_facada.NSXv3Facada()
    nsxv3.setup()
    vsphere = vsphere_client.VSphereClient()

    agent = ca.CommonAgentLoop(
        NSXv3Manager(nsxv3=nsxv3, vsphere=vsphere),
        cfg.CONF.AGENT.polling_interval,
        cfg.CONF.AGENT.quitting_rpc_timeout,
        nsxv3_constants.NSXV3_AGENT_TYPE,
        nsxv3_constants.NSXV3_BIN
    )

    LOG.info("Activate runtime migration from ML2 DVS driver=%s",
             is_migration_enabled())
    LOG.info("VMware NSXv3 Agent initialized successfully.")
    service.launch(cfg.CONF, agent, restart_method='mutate').wait()
