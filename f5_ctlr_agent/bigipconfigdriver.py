#!/usr/bin/env python

# Copyright (c) 2018-2021 F5 Networks, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import argparse
import fcntl
import hashlib
import json
import logging
import os
import os.path
import signal
import socket
import sys
import threading
import time
import traceback
import copy
import pyinotify

from urllib.parse import urlparse
from f5_cccl.api import F5CloudServiceManager
from f5_cccl.exceptions import F5CcclError
from f5_cccl.utils.mgmt import mgmt_root
from f5_cccl.utils.profile import (delete_unused_ssl_profiles,
                                   create_client_ssl_profile,
                                   create_server_ssl_profile)

from f5.bigip import ManagementRoot

log = logging.getLogger(__name__)
console = logging.StreamHandler()
console.setFormatter(
    logging.Formatter("[%(asctime)s %(name)s %(levelname)s] %(message)s"))
root_logger = logging.getLogger()
root_logger.addHandler(console)


class ResponseStatusFilter(logging.Filter):
    def filter(self, record):
        return not record.getMessage().startswith("RESPONSE::STATUS")


class CertFilter(logging.Filter):
    def filter(self, record):
        return "CERTIFICATE" not in record.getMessage()


class KeyFilter(logging.Filter):
    def filter(self, record):
        return "PRIVATE KEY" not in record.getMessage()


root_logger.addFilter(ResponseStatusFilter())
root_logger.addFilter(CertFilter())
root_logger.addFilter(KeyFilter())


DEFAULT_LOG_LEVEL = logging.INFO
DEFAULT_VERIFY_INTERVAL = 30.0
NET_SCHEMA_NAME = 'cccl-net-api-schema.yml'


class CloudServiceManager():
    """CloudServiceManager class.

    Applies a configuration to a BigIP

    Args:
        bigip: ManagementRoot object
        partition: BIG-IP partition to manage
    """

    def __init__(self, bigip, partition, user_agent=None, prefix=None,
                 schema_path=None,gtm=False):
        """Initialize the CloudServiceManager object."""
        self._mgmt_root = bigip
        self._schema = schema_path
        self._is_gtm = gtm
        if gtm:
            self._gtm = GTMManager(
                bigip,
                partition,
                user_agent=user_agent)
            self._cccl=None
        else:
            self._cccl = F5CloudServiceManager(
                bigip,
                partition,
                user_agent=user_agent,
                prefix=prefix,
                schema_path=schema_path)
            self._gtm=None

    def is_gtm(self):
        """ Return is gtm config"""
        return self._is_gtm

    def mgmt_root(self):
        """ Return the BIG-IP ManagementRoot object"""
        return self._mgmt_root

    def get_partition(self):
        """ Return the managed partition."""
        return self._cccl.get_partition()

    def get_schema_type(self):
        """Return 'ltm' or 'net', based on schema type."""
        if self._schema is None:
            return 'ltm'
        elif 'net' in self._schema:
            return 'net'

    def _apply_ltm_config(self, config):
        """Apply the ltm configuration to the BIG-IP.

        Args:
            config: BIG-IP config dict
        """
        return self._cccl.apply_ltm_config(config)

    def _apply_net_config(self, config):
        """Apply the net configuration to the BIG-IP."""
        return self._cccl.apply_net_config(config)

    def get_proxy(self):
        """Called from 'CCCL' delete_unused_ssl_profiles"""
        return self._cccl.get_proxy()


class IntervalTimerError(Exception):
    def __init__(self, msg):
        Exception.__init__(self, msg)


class IntervalTimer(object):
    def __init__(self, interval, cb):
        float(interval)
        if 0 >= interval:
            raise IntervalTimerError("interval must be greater than 0")

        if not cb or not callable(cb):
            raise IntervalTimerError("cb must be callable object")

        self._cb = cb
        self._interval = interval
        self._execution_time = 0.0
        self._running = False
        self._timer = None
        self._lock = threading.RLock()

    def _set_execution_time(self, start_time, stop_time):
        if stop_time >= start_time:
            self._execution_time = stop_time - start_time
        else:
            self._execution_time = 0.0

    def _adjust_interval(self):
        adjusted_interval = self._interval - self._execution_time
        if adjusted_interval < 0.0:
            adjusted_interval = 0.0
        self._execution_time = 0.0
        return adjusted_interval

    def _run(self):
        start_time = time.process_time()
        try:
            self._cb()
        except Exception as e:
            log.exception(f'Unexpected error: {str(e)}')
        finally:
            with self._lock:
                stop_time = time.process_time()
                self._set_execution_time(start_time, stop_time)
                if self._running:
                    self.start()

    def is_running(self):
        return self._running

    def start(self):
        with self._lock:
            if self._running:
                # restart timer, possibly with a new interval
                self.stop()
            self._timer = threading.Timer(self._adjust_interval(), self._run)
            # timers can't be stopped, cancel just prevents the callback from
            # occuring when the timer finally expires.  Make it a daemon allows
            # cancelled timers to exit eventually without a need for join.
            self._timer.daemon = True
            self._timer.start()
            self._running = True

    def stop(self):
        with self._lock:
            if self._running:
                self._timer.cancel()
                self._timer = None
                self._running = False


class ConfigError(Exception):
    def __init__(self, msg):
        Exception.__init__(self, msg)


def create_ltm_config(partition, config):
    """Extract a BIG-IP configuration from the LTM configuration.

    Args:
        config: BigIP config
    """
    ltm = {}
    if 'resources' in config and partition in config['resources']:
        ltm = config['resources'][partition]

    return ltm

def get_gtm_config(config):
    """Extract a BIG-IP configuration from the GTM configuration.

    Args:
        config: BigIP config
    """
    gtm = {}
    if 'gtm' in config:
        gtm = config['gtm']

    return gtm

def create_network_config(config):
    """Extract a BIG-IP Network configuration from the network config.

    Args:
        config: BigIP config which contains vxlan defs
    """
    net = {}
    if ('static-routes' in config and 'routes' in config['static-routes']
            and config['static-routes']['routes'] is not None):
        net['routes'] = config['static-routes']['routes']
        # Only set cis-identifier if it's not empty
        if 'cis-identifier' in config['static-routes'] and config['static-routes']['cis-identifier']:
            net['cis-identifier'] = config['static-routes']['cis-identifier']
    if 'vxlan-fdb' in config:
        net['userFdbTunnels'] = [config['vxlan-fdb']]
    # Add ARPs only if disable-arp is set to false
    if not _is_arp_disabled(config) and ('vxlan-arp' in config and 'arps' in config['vxlan-arp']
            and config['vxlan-arp']['arps'] is not None):
        net['arps'] = config['vxlan-arp']['arps']
    else:
        #Disabling logging ARP entries.
        log.debug("NET Config: %s", json.dumps(net))
    return net


def _create_custom_profiles(mgmt, partition, custom_profiles):
    incomplete = 0

    # Server profiles may reference a CA cert in another server profile.
    # These need to be loaded first.
    for profile in custom_profiles:
        caFile = profile.get('caFile', '')
        if profile['context'] == 'serverside' and caFile == "self":
            incomplete += create_server_ssl_profile(mgmt, partition, profile)

    for profile in custom_profiles:
        if profile['context'] == 'clientside':
            incomplete += create_client_ssl_profile(mgmt, partition, profile)
        elif profile['context'] == 'serverside':
            caFile = profile.get('caFile', '')
            if caFile != "self":
                incomplete += create_server_ssl_profile(
                    mgmt, partition, profile)
        else:
            log.error(
                "Only client or server custom profiles are supported.")

    return incomplete


def _delete_unused_ssl_profiles(mgr, partition, config):
    return delete_unused_ssl_profiles(mgr, partition, config)


class ConfigHandler():
    def __init__(self, config_file, managers, verify_interval):
        self._config_file = config_file
        self._managers = managers

        self._condition = threading.Condition()
        self._thread = threading.Thread(target=self._do_reset)
        self._pending_reset = False
        self._stop = False
        self._backoff_time = 1
        self._backoff_timer = None
        self._max_backoff_time = 128

        self._verify_interval = verify_interval
        self._interval = IntervalTimer(self._verify_interval,
                                       self.notify_reset)
        self._thread.start()

    def stop(self):
        self._condition.acquire()
        self._stop = True
        self._condition.notify()
        self._condition.release()
        if self._backoff_timer is not None:
            self.cleanup_backoff()

    def notify_reset(self):
        self._condition.acquire()
        self._pending_reset = True
        self._condition.notify()
        self._condition.release()

    def _do_reset(self):
        log.debug('config handler thread start')

        with self._condition:
            while True:
                self._condition.acquire()
                if not self._pending_reset and not self._stop:
                    self._condition.wait()
                log.debug('config handler woken for reset')

                self._pending_reset = False
                self._condition.release()

                if self._stop:
                    log.info('stopping config handler')
                    if self._backoff_timer is not None:
                        self.cleanup_backoff()
                    break

                start_time = time.time()

                incomplete = 0
                try:
                    config = _parse_config(self._config_file)

                    # If LTM is not disabled - CCCL mode and
                    # No 'resources' indicates that the controller is not
                    # yet ready -- it does not mean to apply an empty config
                    if not _is_ltm_disabled(config) and 'resources' not in config:
                        continue

                    # No ARP entries indicate controller is not yet ready
                    # Valid even when there are no resources in cluster mode environment
                    # No FDB entries indicate controller is not yet ready.
                    if not _is_arp_disabled(config) and ('vxlan-arp' not in config or 'vxlan-fdb' not in config):
                        continue

                    # No route entries indicate controller is not yet ready in static route mode.
                    if _is_static_routing_enabled(config) and 'static-routes' not in config:
                        continue

                    # In CIS secondary mode if primary cluster status is up, cccl config
                    # should not be pushed by secondary CIS
                    if _is_cis_secondary(config) and _is_primary_cluster_status_up(config):
                        continue

                    # In CIS arbitrator mode, cccl config should not be pushed by arbitrator CIS
                    # if its not the active leader
                    if _is_cis_in_arbitrator_mode(config) and not _is_leader(config):
                        log.debug("CIS in arbitrator mode and not the leader, skipping cccl config push")
                        continue
                    incomplete = self._update_cccl(config)

                except ValueError:
                    formatted_lines = traceback.format_exc().splitlines()
                    last_line = formatted_lines[-1]
                    log.error('Failed to process the config file {} ({})'
                              .format(self._config_file, last_line))
                    incomplete = 1
                except Exception as e:
                    log.exception(f'Unexpected error: {str(e)}')
                    incomplete = 1

                gtmIncomplete = 0
                try:
                    config = _parse_config(self._config_file)
                    gtmIncomplete=self._update_gtm(config)
                except ValueError:
                    gtmIncomplete += 1
                    formatted_lines = traceback.format_exc().splitlines()
                    last_line = formatted_lines[-1]
                    log.error('Failed to process the config file {} ({})'
                              .format(self._config_file, last_line))
                except Exception as e:
                    log.exception(f'Unexpected error: {str(e)}')
                    gtmIncomplete = 1

                if incomplete|gtmIncomplete:
                    # Error occurred, perform retries
                    self.handle_backoff()
                else:
                    if (self._interval and self._interval.is_running()
                            is False):
                        self._interval.start()
                    self._backoff_time = 1
                    if self._backoff_timer is not None:
                        self.cleanup_backoff()

                perf_enable = os.environ.get('SCALE_PERF_ENABLE')
                if perf_enable:  # pragma: no cover
                    test_data = {}
                    app_count = 0
                    backend_count = 0
                    for service in config['resources']['test'][
                            'virtualServers']:
                        app_count += 1
                        backends = 0
                        for pool in config['resources']['test']['pools']:
                            if service['name'] in pool['name']:
                                backends = len(pool['members'])
                                break
                        test_data[service['name']] = backends
                        backend_count += backends
                    test_data['Total_Services'] = app_count
                    test_data['Total_Backends'] = backend_count
                    test_data['Time'] = time.time()
                    json_data = json.dumps(test_data)
                    log.info('SCALE_PERF: Test data: %s',
                             json_data)

                log.debug('updating tasks finished, took %s seconds',
                          time.time() - start_time)

        if self._interval:
            self._interval.stop()

    def _update_gtm(self, config):
        gtmIncomplete=0
        for mgr in self._managers:
            if mgr.is_gtm():
                oldGtmConfig = mgr._gtm.get_gtm_config()
                # partition = mgr._gtm.get_partition()
                partition="Common"
                try:
                    allConfig=get_gtm_config(config)
                    if bool(allConfig):
                        newGtmConfig = allConfig["config"]
                        self._deleted_tenants = allConfig["deletedTenants"]
                        mgr._gtm.pre_process_gtm(newGtmConfig)
                        isConfigSame = sorted(oldGtmConfig.items())==sorted(newGtmConfig.items())
                        if not isConfigSame and len(oldGtmConfig)==0:
                            # GTM config is not same and for
                            # first time gtm config updates
                            if partition in newGtmConfig:
                                #Remove unused GTM PoolMembers from BIGIP created by CIS <= v2.7.1
                                mgr._gtm.remove_unused_poolmembers(partition, newGtmConfig[partition])
                                mgr._gtm.create_gtm(
                                        partition,
                                        newGtmConfig)
                                # mgr._gtm.delete_update_gtm(
                                #         partition,
                                #         newGtmConfig, newGtmConfig)
                            mgr._gtm.replace_gtm_config(allConfig)
                        elif not isConfigSame:
                            # GTM config is not same
                            log.info("New changes observed in gtm config")
                            if partition in newGtmConfig:
                                mgr._gtm.delete_update_gtm(
                                        partition,
                                        newGtmConfig)
                            mgr._gtm.replace_gtm_config(allConfig)

                except F5CcclError as e:
                    # We created an invalid configuration, raise the
                    # exception and fail
                    log.error("GTM Error.....:%s",e.msg)
                    gtmIncomplete += 1
        return gtmIncomplete

    def _update_cccl(self, config):
        _handle_vxlan_config(config)
        cfg_net = create_network_config(config)
        incomplete = 0
        for mgr in self._managers:
            if mgr.is_gtm():
                continue
            partition = mgr.get_partition()
            cfg_ltm = create_ltm_config(partition, config)
            try:
                # Manually create custom profiles;
                # CCCL doesn't yet do this
                if 'customProfiles' in cfg_ltm and \
                        mgr.get_schema_type() == 'ltm':
                    tmp = 0
                    tmp = _create_custom_profiles(
                        mgr.mgmt_root(),
                        partition,
                        cfg_ltm['customProfiles'])
                    incomplete += tmp

                # Apply the BIG-IP config after creating profiles
                # and before deleting profiles
                if mgr.get_schema_type() == 'net':
                    incomplete += mgr._apply_net_config(cfg_net)
                else:
                    incomplete += mgr._apply_ltm_config(cfg_ltm)

                # Manually delete custom profiles (if needed)
                if mgr.get_schema_type() == 'ltm':
                    _delete_unused_ssl_profiles(
                        mgr,
                        partition,
                        cfg_ltm)

            except F5CcclError as e:
                # We created an invalid configuration, raise the
                # exception and fail
                log.error("CCCL Error: %s", e.msg)
                incomplete += 1

        return incomplete

    def cleanup_backoff(self):
        """Cleans up canceled backoff timers."""
        self._backoff_timer.cancel()
        self._backoff_timer.join()
        self._backoff_timer = None

    def handle_backoff(self):
        """Wrapper for calls to retry_backoff."""
        if (self._interval and self._interval.is_running() is
                True):
            self._interval.stop()
        if self._backoff_timer is None:
            self.retry_backoff()

    def retry_backoff(self):
        """Add a backoff timer to retry in case of failure."""
        def timer_cb():
            self._backoff_timer = None
            self.notify_reset()

        self._backoff_timer = threading.Timer(
            self._backoff_time, timer_cb
        )
        log.error("Error applying config, will try again in %s seconds",
                  self._backoff_time)
        self._backoff_timer.start()
        if self._backoff_time < self._max_backoff_time:
            self._backoff_time *= 2


class ConfigWatcher(pyinotify.ProcessEvent):
    def __init__(self, config_file, on_change):
        basename = os.path.basename(config_file)
        if not basename or 0 == len(basename):
            raise ConfigError('config_file must be a file path')

        self._config_file = config_file
        self._on_change = on_change

        self._config_dir = os.path.dirname(self._config_file)
        self._config_stats = None
        if os.path.exists(self._config_file):
            try:
                self._config_stats = self._digest()
            except IOError as ioe:
                log.warning('ioerror during sha sum calculation: {}'.
                            format(ioe))

        self._running = False
        self._polling = False
        self._user_abort = False
        signal.signal(signal.SIGINT, self._exit_gracefully)
        signal.signal(signal.SIGTERM, self._exit_gracefully)

    def _exit_gracefully(self, signum, frame):
        self._user_abort = True
        self._running = False

    def _loop_check(self, notifier):
        if self._polling:
            log.debug('inotify loop ended - returning to polling mode')
            return True
        else:
            return False

    def loop(self):
        self._running = True
        if not os.path.exists(self._config_dir):
            log.info(
                'configured directory doesn\'t exist {}, entering poll loop'.
                format(self._config_dir))
            self._polling = True

        while self._running:
            try:
                while self._polling:
                    if self._polling:
                        if os.path.exists(self._config_dir):
                            log.debug('found watchable directory - {}'.format(
                                self._config_dir))
                            self._polling = False
                            break
                        else:
                            log.debug('waiting for watchable directory - {}'.
                                      format(self._config_dir))
                            time.sleep(1)

                _wm = pyinotify.WatchManager()
                _notifier = pyinotify.Notifier(_wm, default_proc_fun=self)
                _notifier.coalesce_events(True)
                mask = (pyinotify.IN_CREATE | pyinotify.IN_DELETE |
                        pyinotify.IN_MOVED_FROM | pyinotify.IN_MOVED_TO |
                        pyinotify.IN_CLOSE_WRITE | pyinotify.IN_MOVE_SELF |
                        pyinotify.IN_DELETE_SELF)
                _wm.add_watch(
                    path=self._config_dir,
                    mask=mask,
                    quiet=False,
                    exclude_filter=lambda path: False)

                log.info('entering inotify loop to watch {}'.format(
                    self._config_file))
                _notifier.loop(callback=self._loop_check)

                if (not self._polling and _notifier._fd is None):
                    log.info('terminating')
                    self._running = False
            except Exception as e:
                log.warning(e)

        if self._user_abort:
            log.info('Received user kill signal, terminating.')

    def _digest(self):
        sha = hashlib.sha256()

        with open(self._config_file, 'rb') as f:
            fcntl.lockf(f.fileno(), fcntl.LOCK_SH, 0, 0, 0)
            while True:
                buf = f.read(4096)
                if not buf:
                    break
                sha.update(buf)
            fcntl.lockf(f.fileno(), fcntl.LOCK_UN, 0, 0, 0)
        return sha.digest()

    def _should_watch(self, pathname):
        if pathname == self._config_file:
            return True
        return False

    def _is_changed(self):
        changed = False
        cur_hash = None
        if not os.path.exists(self._config_file):
            if cur_hash != self._config_stats:
                changed = True
            else:
                changed = False
        else:
            try:
                cur_hash = self._digest()
                if cur_hash != self._config_stats:
                    changed = True
                else:
                    changed = False
            except IOError as ioe:
                log.warning('ioerror during sha sum calculation: {}'.
                            format(ioe))

        return (changed, cur_hash)

    def process_default(self, event):
        if (pyinotify.IN_DELETE_SELF == event.mask or
                pyinotify.IN_MOVE_SELF == event.mask):
            log.warn(
                'watchpoint {} has been moved or destroyed, using poll loop'.
                format(self._config_dir))
            self._polling = True

            if self._config_stats is not None:
                log.debug('config file {} changed, parent gone'.format(
                    self._config_file))
                self._config_stats = None
                self._on_change()

        if self._should_watch(event.pathname):
            (changed, sha) = self._is_changed()

            if changed:
                log.debug('config file {0} changed - signalling bigip'.format(
                    self._config_file, self._config_stats, sha))
                self._config_stats = sha
                self._on_change()

class GTMManager(object):
    """F5 Common Controller Cloud Service Management.

    The F5 Common Controller Core Library (CCCL) is an orchestration package
    that provides a declarative API for defining BIG-IP LTM and NET services
    in diverse environments (e.g. Marathon, Kubernetes, OpenStack). The
    API will allow a user to create proxy services by specifying the:
    virtual servers, pools, L7 policy and rules, monitors, arps, or fdbTunnels
    as a service description object.  Each instance of the CCCL is initialized
    with namespace qualifiers to allow it to uniquely identify the resources
    under its control.
    """

    def __init__(self, bigip, partition, user_agent=None):
        """Initialize an instance of the F5 CCCL service manager.

        :param bigip: BIG-IP management root.
        :param partition: Name of BIG-IP partition to manage.
        :param user_agent: String to append to the User-Agent header for
        iControl REST requests (default: None)
        :param prefix:  The prefix assigned to resources that should be
        managed by this CCCL instance.  This is prepended to the
        resource name (default: None)
        :param schema_path: User defined schema (default: from package)
        """
        log.debug("F5GTMManager initialize")

        # Set user-agent for ICR session
        if user_agent is not None:
            bigip.icrs.append_user_agent(user_agent)
        self._user_agent = user_agent
        self._mgmt_root = bigip
        self._partition = partition
        self._gtm_config = {}
        self._active_tenants = []
        self._deleted_tenants = []
        self._gtm = bigip.tm.gtm

    def get_gtm_config(self):
        """ Return the GTM config object"""
        return self._gtm_config

    def replace_gtm_config(self, config):
        """ Updating the GTM config object"""
        self._active_tenants = config["activeTenants"]
        self._deleted_tenants = []
        self._gtm_config = config["config"]

    @staticmethod
    def format_server_name(dataserver_ip):
        """ Format GSLB server name from DataServer IP
        
        Supports IPv4, IPv6, and route domains.
        Replaces '.', ':', and '%' with '_' for valid BIG-IP server names.
        
        Args:
            dataserver_ip: DataServer IP address
            
        Returns:
            Formatted server name
            
        Examples:
            "10.155.15.101"     → "server_10_155_15_101"
            "2001:db8::1"       → "server_2001_db8__1"
            "10.1.1.1%2"        → "server_10_1_1_1_2"
            "2001:db8::1%2"     → "server_2001_db8__1_2"
        """
        return "server_{}".format(
            dataserver_ip.replace(".", "_")
                         .replace(":", "_")
                         .replace("%", "_")
        )

    def _parse_member_spec(self, member_spec, pool_dataserver=None):
        """Centralized member spec parsing — single source of truth.
        
        Replaces duplicated parsing logic in:
        - extract_dataservers()
        - build_virtual_server_inventory()
        - create_gtm() Step 2
        - cleanup methods
        - _convert_member_to_bigip_reference()
        
        Args:
            member_spec: Member string from config (e.g., '10.1.1.1|10.2.2.2|80')
            pool_dataserver: Optional pool-level DataServer
            
        Returns:
            Tuple: (dataserver, member_ip, member_port, destination) or
                   (None, None, None, None) if invalid
        """
        # Auto-detect separator: | for IPv6, : for backward compat IPv4
        separator = '|' if '|' in member_spec else ':'
        parts = member_spec.split(separator)
        
        if len(parts) == 3:
            # Format: DataServer|IP|port or DataServer:IP:port
            dataserver, member_ip, member_port = parts
        elif len(parts) == 2:
            # Format: IP|port or IP:port (uses pool DataServer)
            if not pool_dataserver:
                log.warning("GTM: Member '{}' has no DataServer".format(member_spec))
                return None, None, None, None
            dataserver = pool_dataserver
            member_ip, member_port = parts
        else:
            log.warning("GTM: Invalid member format: {}".format(member_spec))
            return None, None, None, None
        
        destination = "{}:{}".format(member_ip, member_port)
        return dataserver, member_ip, member_port, destination

    @staticmethod
    def _format_vs_name(destination):
        """Generate a BIG-IP-safe virtual server name from a destination.
        
        Replaces duplicated formatting in multiple methods.
        
        Args:
            destination: IP:port string (e.g., '10.2.2.2:80' or '2001:db8::1:443')
            
        Returns:
            Sanitized VS name (e.g., 'vs-10-2-2-2-80' or 'vs-2001-db8--1-443')
        """
        return "vs-{}".format(
            destination.replace(".", "-")
                       .replace(":", "-")
                       .replace("%", "-")
        )

    def _parse_gtm_config_once(self, gtmConfig, partition):
        """Single-pass config parsing to extract ALL needed data structures.
        
        This replaces 5 separate iteration loops:
        - extract_dataservers()
        - build_virtual_server_inventory()
        - create_gtm() Step 2 (expected_members)
        - cleanup_unused_virtual_servers() old config
        - cleanup_unused_virtual_servers() new config
        
        Args:
            gtmConfig: GTM configuration dict
            partition: Partition name
            
        Returns:
            dict with keys:
            - 'dataservers': set of unique DataServer IPs
            - 'vs_inventory': dict {server_name -> set((member_ip, vs_name, destination))}
            - 'members_by_pool': dict {pool_name -> set(member_bigip_refs)}
            - 'all_member_refs': set of all BIG-IP member references
            - 'all_server_names': set of all server names
        """
        result = {
            'dataservers': set(),
            'vs_inventory': {},
            'members_by_pool': {},
            'all_member_refs': set(),
            'all_server_names': set(),
        }
        
        if partition not in gtmConfig:
            return result
        
        wideips = gtmConfig[partition].get('wideIPs', [])
        if not wideips:
            return result
        
        for wideip in wideips:
            pools = wideip.get('pools', [])
            if not pools:
                continue
            
            for pool in pools:
                pool_name = pool.get('name')
                pool_dataserver = pool.get('DataServer')
                members = pool.get('members', [])
                
                if pool_name and pool_name not in result['members_by_pool']:
                    result['members_by_pool'][pool_name] = set()
                
                if not members:
                    continue
                
                for member_spec in members:
                    # Centralized parsing (replaces 5 duplicated blocks)
                    dataserver, member_ip, member_port, destination = \
                        self._parse_member_spec(member_spec, pool_dataserver)
                    
                    if dataserver is None:
                        continue  # Invalid format, already logged
                    
                    # 1. Dataservers
                    result['dataservers'].add(dataserver)
                    
                    # 2. VS inventory
                    server_name = self.format_server_name(dataserver)
                    vs_name = self._format_vs_name(destination)
                    
                    if server_name not in result['vs_inventory']:
                        result['vs_inventory'][server_name] = set()
                    result['vs_inventory'][server_name].add(
                        (member_ip, vs_name, destination))
                    
                    # 3. Member references
                    member_ref = "{}:{}".format(server_name, vs_name)
                    if pool_name:
                        result['members_by_pool'][pool_name].add(member_ref)
                    result['all_member_refs'].add(member_ref)
                    result['all_server_names'].add(server_name)
        
        return result

    def mgmt_root(self):
        """ Return the BIG-IP ManagementRoot object"""
        return self._mgmt_root

    def gtm(self):
        return self._gtm

    def get_partition(self):
        """ Return the managed partition."""
        return self._partition

    @staticmethod
    def pre_process_gtm(gtmConfig):
        for partition in gtmConfig:
            if "wideIPs" in gtmConfig[partition]:
                if gtmConfig[partition]['wideIPs'] is not None:
                    for config in gtmConfig[partition]['wideIPs']:
                        for pool in config['pools']:
                            if "monitors" in pool.keys():
                                for monitor in pool['monitors']:
                                    if "send" in monitor.keys():
                                        monitor["send"] = monitor["send"].replace("\r", "\\r")
                                        monitor["send"] = monitor["send"].replace("\n", "\\n")

    def delete_update_gtm(self,partition,gtmConfig):
        """ Update GTM object in BIG-IP """
        try:
            oldConfig = self._gtm_config
            mgmt = self.mgmt_root()
            gtm=mgmt.tm.gtm
            if partition in oldConfig and partition in gtmConfig:
                opr_config = self.process_config(oldConfig[partition],gtmConfig[partition])
                rev_map = self.create_reverse_map(oldConfig[partition])
                for opr in opr_config:
                    if opr=="delete":
                        self.handle_operation_delete(gtm,partition,opr_config[opr],rev_map)
                    if opr=="create" or opr=="update":
                        self.handle_operation_create(gtm,partition,gtmConfig,opr_config[opr],opr)
        except F5CcclError as e:
            raise e

    def handle_operation_delete(self,gtm,partition,opr_config,rev_map):
        """ Handle delete operation """
        try:
            # Save old config before making changes
            oldConfig = copy.deepcopy(self._gtm_config)
            
            # Parse configs once for cleanup operations
            log.debug("GTM: Parsing configs for delete operation cleanup")
            old_parsed = self._parse_gtm_config_once(oldConfig, partition)
            new_parsed = self._parse_gtm_config_once(self._gtm_config, partition)
            
            # Step 1: Delete monitors
            if len(opr_config["monitors"]) > 0:
                for monitor in opr_config["monitors"]:
                    poolName = rev_map["monitors"][monitor]
                    self.remove_monitor_from_gtm_pool(gtm, partition, poolName, monitor)
                    self.delete_gtm_hm(gtm, partition, monitor)

            # Step 2: Delete pools (this also removes members)
            if len(opr_config["pools"]) > 0:
                for pool in opr_config["pools"]:
                    wideipForPoolDeleted = rev_map["pools"][pool]
                    for wideip in wideipForPoolDeleted:
                        self.delete_gtm_pool(gtm, partition, wideip, pool)
            
            # Step 3: Delete wideIPs
            if len(opr_config["wideIPs"]) > 0:
                for wideip in opr_config["wideIPs"]:
                    self.delete_gtm_wideip(gtm, partition, wideip)
            
            # Step 4: Clean up unused virtual servers (use pre-parsed data)
            log.info("GTM: Cleaning up unused virtual servers")
            self.cleanup_unused_virtual_servers(gtm, partition, oldConfig, self._gtm_config,
                                                 old_parsed=old_parsed, new_parsed=new_parsed)
            
            # Step 5: Clean up unused GSLB servers (use pre-parsed data)
            log.info("GTM: Cleaning up unused GSLB servers")
            # Get datacenter name from config
            datacenter_name = None
            if partition in self._gtm_config:
                datacenter_name = self._gtm_config[partition].get('dataCenter', None)
                if datacenter_name and '/' in datacenter_name:
                    datacenter_name = datacenter_name.split('/')[-1]
            self.cleanup_unused_gslb_servers(gtm, datacenter_name, oldConfig, self._gtm_config,
                                              old_parsed=old_parsed, new_parsed=new_parsed)
            
        except F5CcclError as e:
            log.error("GTM: Error while handling delete operation: %s", e)
            raise e

    def handle_operation_create(self,gtm,partition,gtmConfig,opr_config,opr):
        """ Handle create operation """
        try:
            oldConfig = copy.deepcopy(self._gtm_config)
            
            if len(opr_config["pools"]) > 0 or len(opr_config["monitors"]) > 0 or len(opr_config["wideIPs"]) > 0:
                # Parse configs once for all operations
                log.debug("GTM: Parsing configs for create/update operation")
                old_parsed = self._parse_gtm_config_once(oldConfig, partition)
                new_parsed = self._parse_gtm_config_once(gtmConfig, partition)
                
                # Ensure infrastructure is in place before creating/updating pools
                log.info("GTM: Ensuring infrastructure for create/update operation")
                self.orchestrate_gtm_infrastructure(gtm, partition, gtmConfig, parsed=new_parsed)
                
                if partition in gtmConfig and "wideIPs" in gtmConfig[partition]:
                    if gtmConfig[partition]['wideIPs'] is not None:
                        for config in gtmConfig[partition]['wideIPs']:
                            monitor = ""
                            newPools = dict()
                            for pool in config['pools']:
                                # Pool object
                                newPools[pool['name']] = {
                                    'name': pool['name'], 'partition': partition, 'ratio': 1, 'order': pool['order']
                                }
                                all_monitors = ""
                                if "monitors" in pool.keys():
                                    # Create Health Monitor
                                    for monitor in pool["monitors"]:
                                        if opr == "update" and monitor['name'] in opr_config["monitors"]:
                                            # Delete Old Health monitors
                                            self.remove_monitor_from_gtm_pool(gtm, partition, pool['name'],
                                                                              monitor['name'])
                                            self.delete_gtm_hm(gtm, partition, monitor['name'])
                                        # Create a new Health Monitor
                                        self.create_HM(gtm, partition, monitor, config['name'])
                                        all_monitors += "/" + partition + "/" + monitor['name']
                                        if monitor["name"] != pool["monitors"][-1]["name"]:
                                            all_monitors += " and "
                                # Delete the old pool members
                                if partition in oldConfig and "wideIPs" in oldConfig[partition]:
                                    if oldConfig[partition]['wideIPs'] is not None:
                                        for index, oldWideIP in enumerate(oldConfig[partition]['wideIPs']):
                                            # Match the current wideIP being processed
                                            if oldWideIP['name'] == config['name']:
                                                for pool_index, oldPool in enumerate(oldWideIP['pools']):
                                                    if oldPool['name'] == pool['name']:
                                                        if oldPool['members'] is not None and pool['members'] is not None:
                                                            oldPoolMember = set(oldPool['members'])
                                                            newPoolMember = set(pool['members'])
                                                            deleteMember = oldPoolMember - newPoolMember
                                                            log.info("GTM: Members to delete from pool {}: {}".format(
                                                                pool['name'], deleteMember))
                                                            for member in deleteMember:
                                                                # Convert config format to BIG-IP member reference
                                                                member_ref = self._convert_member_to_bigip_reference(
                                                                    member, oldPool.get('DataServer'))
                                                                log.info("GTM: Deleting member {} (BIG-IP ref: {}) from pool {}".format(
                                                                    member, member_ref, oldPool['name']))
                                                                self.remove_member_to_gtm_pool(
                                                                    gtm,
                                                                    partition,
                                                                    oldPool['name'],
                                                                    member_ref)
                                                            self._gtm_config[partition]['wideIPs'][index]["pools"][
                                                                pool_index]['members'] = None
                            try:
                                # Create GTM pool
                                self.create_gtm_pool(gtm, partition, config, all_monitors)
                                # Create Wideip
                                self.create_wideip(gtm, partition, config, newPools)
                            except F5CcclError as e:
                                raise e
                
                # After all updates, clean up orphaned virtual servers and GSLB servers (use pre-parsed data)
                log.info("GTM: Cleaning up orphaned infrastructure after update")
                self.cleanup_unused_virtual_servers(gtm, partition, oldConfig, gtmConfig,
                                                     old_parsed=old_parsed, new_parsed=new_parsed)
                # Get datacenter name from config
                datacenter_name = None
                if partition in gtmConfig:
                    datacenter_name = gtmConfig[partition].get('dataCenter', None)
                    if datacenter_name and '/' in datacenter_name:
                        datacenter_name = datacenter_name.split('/')[-1]
                self.cleanup_unused_gslb_servers(gtm, datacenter_name, oldConfig, gtmConfig,
                                                  old_parsed=old_parsed, new_parsed=new_parsed)
                
        except F5CcclError as e:
            log.error("GTM: Error while handling create operation: %s", e)
            raise e

    def remove_unused_poolmembers(self, partition, gtmConfig):
        """Remove unused GTM PoolMembers from BIGIP created by CIS <= v2.7.1 """
        try:
            def _get_value(d, k):
                if d[k] is None:
                    return dict()
                return d[k]

            def _get_virtualNames_from_member(gtm_members):
                """ Parse GTM Virtuals from memberNames"""
                list_gtm_virtuals = {}
                for poolName in gtm_members:
                    list_gtm_virtuals[poolName] = []
                    for gtm_member in gtm_members[poolName]:
                        # Handle both old format (with /Shared/) and new format (DataServer:vs_name)
                        if '/Shared/' in gtm_member:
                            # Old format: /Common/server/Shared/vs_name
                            list_gtm_virtuals[poolName].append(gtm_member.split('/Shared/')[1])
                        else:
                            # New format: DataServer:vs_name - skip processing for legacy cleanup
                            log.debug("GTM: Skipping new format member in legacy cleanup: {}".format(gtm_member))
                return list_gtm_virtuals

            def _find_deleted_members(gtm_members,bigip_members):
                del_gtm_members = {}
                # Parse GTM Virtuals from memberNames
                list_gtm_virtuals = _get_virtualNames_from_member(gtm_members)

                # Find all deleted Members from BIGIP for respective Pool
                for poolName in gtm_members:
                    del_gtm_members[poolName] = []
                    for gtm_member in gtm_members[poolName]:
                        # Only process old format members with /Shared/
                        if "ingress_link_" not in gtm_member and '/Shared/' in gtm_member and poolName in bigip_members:
                            gtmPoolObj, gtmMemberName = gtm_member.split('/Shared/')
                            parseSearchStrfromMember = ('_').join(gtmMemberName.split('_')[:-1])

                            extra_bigip_members = list(set(bigip_members[poolName]) - set(list_gtm_virtuals[poolName]))
                            for bigipPoolMember in extra_bigip_members:
                                if bigipPoolMember.startswith(parseSearchStrfromMember):
                                    member = gtmPoolObj + '/Shared/' + bigipPoolMember
                                    del_gtm_members[poolName].append(member)
                return del_gtm_members

            gtm = self.gtm()
            gtm_pools = []
            for wip in _get_value(gtmConfig, "wideIPs"):
                gtm_pools += wip["pools"]


            gtm_members, bigip_members = {}, {}
            # Prepare GTM members from activeConfig and bigip_members from BIGIP based on gtm_members
            for p in gtm_pools:
                if p.get("members"):
                    gtm_members[p['name']] = p["members"]
                    exist = gtm.pools.a_s.a.exists(name=p['name'], partition=partition)
                    if not exist:
                        continue
                    pool = gtm.pools.a_s.a.load(name=p['name'], partition=partition)
                    bigip_members[p['name']] = [gtmMember.name for gtmMember in pool.members_s.get_collection()]

            del_gtm_members = _find_deleted_members(gtm_members,bigip_members)
            try:
                # Remove Members from BIGIP for respective GTM Pool
                for poolName in del_gtm_members:
                    for member in del_gtm_members[poolName]:
                        self.remove_member_to_gtm_pool(
                            gtm,
                            partition,
                            poolName,
                            member)
            except F5CcclError as e:
                log.error("GTM: Error while removing gtm pool member: %s", e)
                raise e
        except F5CcclError as e:
            log.error("GTM: Error while processing for list of pool members to delete: %s", e)
            raise e

    def create_gtm(self, partition, gtmConfig):
        """ Create GTM object in BIG-IP """
        try:
            gtm = self.gtm()
            
            # Step 0: Parse config once for all operations
            log.debug("GTM: Parsing configuration for partition {}".format(partition))
            parsed = self._parse_gtm_config_once(gtmConfig, partition)
            
            # Step 1: Orchestrate infrastructure (datacenter, GSLB servers, virtual servers)
            log.info("GTM: Orchestrating infrastructure for partition {}".format(partition))
            infrastructure = self.orchestrate_gtm_infrastructure(gtm, partition, gtmConfig, parsed=parsed)
            log.debug("GTM: Infrastructure ready: {}".format(infrastructure))
            
            # Step 2: Build expected members from parsed data (already extracted)
            expected_members = parsed['members_by_pool']
            
            # Step 3: Create pools and wideIPs
            if "wideIPs" in gtmConfig[partition]:
                if gtmConfig[partition]['wideIPs'] is not None:
                    for config in gtmConfig[partition]['wideIPs']:
                        newPools = dict()
                        for pool in config['pools']:
                            # Pool object
                            newPools[pool['name']] = {
                                'name': pool['name'], 'partition': partition, 'ratio': 1, 'order': pool['order']
                            }
                            all_monitors = ""
                            if "monitors" in pool.keys():
                                for monitor in pool["monitors"]:
                                    # Create Health Monitor
                                    all_monitors += "/" + partition + "/" + monitor["name"]
                                    if monitor["name"] != pool["monitors"][-1]["name"]:
                                        all_monitors += " and "
                                    self.create_HM(gtm, partition, monitor, config['name'])
                        try:
                            # Create GTM pool
                            self.create_gtm_pool(gtm, partition, config, all_monitors)
                            # Create Wideip
                            self.create_wideip(gtm, partition, config, newPools)
                        except F5CcclError as e:
                            raise e
            
            # Step 4: Clean up orphaned pool members (restart scenario)
            # Check each pool and remove members that shouldn't be there
            log.debug("GTM: Cleaning up orphaned pool members after create")
            for pool_name, expected_member_set in expected_members.items():
                try:
                    if gtm.pools.a_s.a.exists(name=pool_name, partition=partition):
                        pool = gtm.pools.a_s.a.load(name=pool_name, partition=partition)
                        
                        # Get actual members from BIG-IP
                        try:
                            actual_members = set()
                            members_collection = pool.members_s.get_collection()
                            for member in members_collection:
                                actual_members.add(member.name)
                            
                            # Find members to delete (in BIG-IP but not in new config)
                            members_to_delete = actual_members - expected_member_set
                            
                            if members_to_delete:
                                log.debug("GTM: Removing orphaned members from pool {}: {}".format(
                                    pool_name, members_to_delete))
                                
                                for member_name in members_to_delete:
                                    self.remove_member_to_gtm_pool(
                                        gtm, partition, pool_name, member_name)
                        except Exception as e:
                            log.debug("GTM: Could not get members for pool {}: {}".format(
                                pool_name, str(e)))
                except Exception as e:
                    log.warning("GTM: Error cleaning up pool {}: {}".format(pool_name, str(e)))
            
            # Step 5: Clean up orphaned infrastructure (VSs and servers)
            log.debug("GTM: Cleaning up orphaned infrastructure")
            # Save old config before updating (will be empty on restart, but needed for diff)
            old_config = copy.deepcopy(self._gtm_config.get(partition, {}))
            # Update internal config to reflect what we just created
            self._gtm_config[partition] = gtmConfig[partition]
            # Cleanup based on actual BIG-IP state vs new config
            self._cleanup_infrastructure_from_bigip(gtm, partition, expected_members)
            
        except F5CcclError as e:
            log.error("GTM: Error while creating gtm: %s", e)
            raise e
    
    def _cleanup_infrastructure_from_bigip(self, gtm, partition, expected_members):
        """ Clean up VSs and servers by comparing BIG-IP state with expected config
        
        This is used during create_gtm (restart scenario) where we need to clean up
        based on what's actually in BIG-IP, not what's in our old config tracking.
        
        Args:
            gtm: GTM object
            partition: Partition name
            expected_members: Dict of {pool_name: set(member_refs)} that should exist
        """
        try:
            # Build set of all expected members and servers
            all_expected_members = set()
            all_expected_servers = set()
            for pool_name, member_set in expected_members.items():
                all_expected_members.update(member_set)
                for member_ref in member_set:
                    server_name = member_ref.split(':')[0]
                    all_expected_servers.add(server_name)
            
            log.debug("GTM: Expected members after create: {}".format(all_expected_members))
            log.debug("GTM: Expected servers after create: {}".format(all_expected_servers))
            
            # Clean up virtual servers that aren't in expected members
            for server_name in all_expected_servers:
                try:
                    if not gtm.servers.server.exists(name=server_name):
                        continue
                    
                    server = gtm.servers.server.load(name=server_name)
                    
                    # Get all VSs on this server
                    try:
                        vs_list = server.virtual_servers_s.get_collection()
                        for vs in vs_list:
                            member_ref = "{}:{}".format(server_name, vs.name)
                            if member_ref not in all_expected_members:
                                log.debug("GTM: Deleting orphaned VS {} from server {} (restart cleanup)".format(
                                    vs.name, server_name))
                                vs.delete()
                    except Exception as e:
                        log.debug("GTM: Could not enumerate VSs on server {}: {}".format(
                            server_name, str(e)))
                except Exception as e:
                    log.warning("GTM: Error processing server {} for VS cleanup: {}".format(
                        server_name, str(e)))
            
            # Clean up servers with no VSs (that we manage)
            for server_name in list(all_expected_servers):
                try:
                    if not gtm.servers.server.exists(name=server_name):
                        continue
                    
                    server = gtm.servers.server.load(name=server_name)
                    
                    # Check VS count
                    try:
                        vs_list = server.virtual_servers_s.get_collection()
                        vs_count = len(list(vs_list))
                        
                        if vs_count == 0:
                            log.debug("GTM: Deleting server {} with no VSs (restart cleanup)".format(
                                server_name))
                            server.delete()
                    except Exception as e:
                        log.debug("GTM: Could not check VS count for server {}: {}".format(
                            server_name, str(e)))
                except Exception as e:
                    log.warning("GTM: Error cleaning up server {}: {}".format(server_name, str(e)))
                    
        except Exception as e:
            log.error("GTM: Error during infrastructure cleanup from BIG-IP: {}".format(str(e)))

    def create_wideip(self, gtm, partition, config, newPools):
        """ Create wideip and returns the wideip object """
        try:
            exist = gtm.wideips.a_s.a.exists(name=config['name'], partition=partition)
            if not exist:
                log.info('GTM: Creating wideip {}'.format(config['name']))
                gtm.wideips.a_s.a.create(
                    name=config['name'],
                    partition=partition, lastResortPool="none", poolLbMode=config['LoadBalancingMode'])
                # Attach pool to wideip
                self.attach_gtm_pool_to_wideip(gtm, config['name'], partition, list(newPools.values()))
            else:
                wideip = gtm.wideips.a_s.a.load(
                    name=config['name'],
                    partition=partition)
                if wideip.poolLbMode != config['LoadBalancingMode']:
                    wideip.poolLbMode = config['LoadBalancingMode']
                    wideip.update()
                duplicatePools = []
                if hasattr(wideip, 'pools'):
                    for p in newPools.keys():
                        if hasattr(wideip.raw['pools'], p):
                            duplicatePools.append(p)

                for poolName in duplicatePools:
                    del newPools[poolName]

                if len(newPools) > 0:
                    self.attach_gtm_pool_to_wideip(
                        gtm,
                        config['name'],
                        partition,
                        list(newPools.values()))
        except F5CcclError as e:
            log.error("GTM: Error while creating wideip: %s", e)
            raise e


    def create_gtm_pool(self, gtm, partition, config, monitors):
        """ Create gtm pools """
        try:
            for pool in config['pools']:
                exist = gtm.pools.a_s.a.exists(name=pool['name'], partition=partition)
                if not exist:
                    # Create pool object
                    log.info('GTM: Creating Pool: {}'.format(pool['name']))
                    pl = gtm.pools.a_s.a.create(
                        name=pool['name'],
                        partition=partition,fallbackMode=pool['fallbackMode'],loadBalancingMode=pool['LoadBalancingMode'])
                else:
                    pl = gtm.pools.a_s.a.load(
                        name=pool['name'],
                        partition=partition)
                # Updating the monitors
                if monitors != "":
                    pl.monitor = monitors
                    pl.update()
                    log.debug('GTM: Updating monitors for pool {}'.format(pool['name']))
                if pl.fallbackMode !=  pool['fallbackMode']:
                    pl.fallbackMode = pool['fallbackMode']
                    pl.update()
                    log.debug('GTM: Updating fallbackMode for pool {}'.format(pool['name']))
                if pl.loadBalancingMode != pool['LoadBalancingMode']:
                    pl.loadBalancingMode = pool['LoadBalancingMode']
                    pl.update()
                    log.debug('GTM: Updating loadBalancingMode for pool {}'.format(pool['name']))
                if bool(pool['members']):
                    pool_dataserver = pool.get('DataServer', '')
                    
                    for member_spec in pool['members']:
                        # Parse member format - auto-detect separator
                        # New format: DataServer|IP|port or IP|port (with | for IPv6)
                        # Old format: DataServer:IP:port or IP:port (with : for backward compat IPv4)
                        if '|' in member_spec:
                            separator = '|'
                        else:
                            separator = ':'
                        
                        parts = member_spec.split(separator)
                        
                        if len(parts) == 3:
                            # Format: DataServer|IP|port or DataServer:IP:port (per-member DataServer)
                            dataserver = parts[0]
                            member_ip = parts[1]
                            member_port = parts[2]
                            destination = "{}:{}".format(member_ip, member_port)
                        elif len(parts) == 2:
                            # Format: IP|port or IP:port (uses pool DataServer)
                            if not pool_dataserver:
                                log.warning("GTM: Member '{}' in pool {} has no DataServer. Skipping.".format(
                                    member_spec, pool['name']))
                                continue
                            dataserver = pool_dataserver
                            member_ip = parts[0]
                            member_port = parts[1]
                            destination = "{}:{}".format(member_ip, member_port)
                        else:
                            # Legacy format or invalid
                            log.warning("GTM: Unrecognized member format '{}' in pool {}. Using as-is.".format(
                                member_spec, pool['name']))
                            # Keep as-is for legacy compatibility
                            self.add_member_to_gtm_pool(
                                gtm, pl, pool['name'], member_spec, partition)
                            continue
                        
                        # Generate virtual server name (sanitize for BIG-IP name)
                        vs_name = "vs-{}".format(
                            destination.replace(".", "-")
                                       .replace(":", "-")
                                       .replace("%", "-")
                        )
                        # Format server name as server_<ip> (handles IPv4/IPv6/route domains)
                        server_name = self.format_server_name(dataserver)
                        member_name = "{}:{}".format(server_name, vs_name)
                        
                        # Add member to pool
                        self.add_member_to_gtm_pool(
                            gtm, pl, pool['name'], member_name, partition)
        except F5CcclError as e:
            log.error("GTM: Error while creating pool: %s", e)
            raise e

    def attach_gtm_pool_to_wideip(self, gtm, name, partition, poolObj):
        """ Attach gtm pool to the wideip """
        #wideip.raw['pools'] =
        #[{'name': 'api-pool1', 'partition': 'test', 'order': 2, 'ratio': 1}]
        try:
            wideip = gtm.wideips.a_s.a.load(name=name, partition=partition)
            if wideip.lastResortPool == "":
                wideip.lastResortPool = "none"
            if hasattr(wideip, 'pools'):
                wideip.pools.extend(poolObj)
                log.info('GTM: Attaching Pool: {} to wideip {}'.format(poolObj, name))
                try:
                    wideip.update()
                except F5CcclError as e:
                    log.error("GTM: Error while Updating gtm pool to wideip: %s", e)
                    raise e
            else:
                wideip.raw['pools'] = poolObj
                log.info('GTM: Attaching Pool: {} to wideip {}'.format(poolObj, name))
                try:
                    wideip.update()
                except F5CcclError as e:
                    log.error("GTM: Error while Updating gtm pool to wideip: %s", e)
                    raise e
        except F5CcclError as e:
            log.error("GTM: Error while attaching gtm pool to wideip: %s", e)
            raise e

    def remove_monitor_from_gtm_pool(self,gtm,partition,poolName,monitorName):
        """ Remove monitor from gtm pool """
        try:
            pool = gtm.pools.a_s.a.load(name=poolName,partition=partition)
            if hasattr(pool,'monitor'):
                if f"/{partition}/{monitorName}" in pool.monitor:
                    monitors = pool.monitor.split(" and ")
                    monitors.remove(f"/{partition}/{monitorName}")
                    pool.monitor = " and ".join(monitors)
                    pool.update()
                    log.debug("GTM: Detached monitor {} from pool {}".format(monitorName,poolName))
        except F5CcclError as e:
            log.error("Error while removing monitor from pool: %s", e)
            raise e

    def add_member_to_gtm_pool(self, gtm, pool, poolName, memberName, partition):
        """ Add member to gtm pool """
        try:
            if not bool(pool):
                pool = gtm.pools.a_s.a.load(name=poolName,partition=partition)
            exist = pool.members_s.member.exists(
                name=memberName)
            if not exist:
                s = memberName.split(":")
                server = s[0].split("/")[-1]
                vs_name = s[1]
                serverExist = gtm.servers.server.exists(name=server)
                if serverExist:
                    sl = gtm.servers.server.load(name=server)
                    
                    vsExist = sl.virtual_servers_s.virtual_server.exists(
                        name=vs_name)
                    if vsExist:
                        # Check if member already exists (stored as server:vs without /Common/ prefix)
                        pmExist = pool.members_s.member.exists(
                            name=memberName,
                            partition="Common")
                        
                        if not pmExist:
                            # Add member to gtm pool
                            # Using product='bigip' allows standard format with partition
                            pool.members_s.member.create(name=memberName, partition="Common")
                            log.info('GTM: Added member {} to pool {}'.format(memberName, poolName))
                    else:
                        raise F5CcclError(
                            msg="Virtual Server Resource not Available in BIG-IP")
                else:
                    # Delete pool for invalid server config
                    pool = gtm.pools.a_s.a.load(name=poolName, partition=partition)
                    pool.delete()
                    raise F5CcclError(msg="Server Resource not Available in BIG-IP")
        except (F5CcclError) as e:
            log.debug("GTM: Error while adding member to pool.")
            raise e


    def get_bigip_version(self):
        try:
            mgmt= self.mgmt_root()
            verList = mgmt.tmos_version.split('.')
            return float(verList[0] + '.' + verList[1])
        except F5CcclError as e:
            log.error("GTM: Could not fetch BigipVersion: %s", e)
            raise e

    def create_gslb_server(self, gtm, server_name, datacenter_name, addresses, 
                          product='generic-host', virtual_server_discovery='disabled',
                          description=None, monitor=None):
        """ Create GTM GSLB server
        
        Args:
            gtm: GTM object from BIG-IP management root
            server_name: Name of the GSLB server to create
            datacenter_name: Name of the datacenter the server belongs to
            addresses: List of IP addresses for the server. Can be a list of dicts 
                      [{'name': 'ip_addr', 'translation': 'nat_ip'}] or simple list ['ip1', 'ip2']
            product: Product type (default: 'generic-host'). Options: 'bigip', 'generic-host', etc.
            virtual_server_discovery: Discovery mode (default: 'disabled'). Options: 'enabled', 'disabled', 'enabled-no-delete'
            description: Optional description of the server
            monitor: Optional health monitor (e.g., '/Common/bigip')
            
        Returns:
            server object if created or loaded successfully
            
        Raises:
            F5CcclError: If server creation fails
        """
        try:
            # Check if server already exists
            server_exists = gtm.servers.server.exists(name=server_name)
            
            if server_exists:
                log.debug("GTM: GSLB Server {} already exists".format(server_name))
                server = gtm.servers.server.load(name=server_name)
            else:
                log.info("GTM: Creating GSLB server {}".format(server_name))
                
                # Build creation parameters
                create_params = {
                    'name': server_name,
                    'datacenter': datacenter_name,
                    'product': product,
                    'virtualServerDiscovery': virtual_server_discovery
                }
                
                # Process addresses - convert to proper format if needed
                if addresses:
                    if isinstance(addresses[0], dict):
                        create_params['addresses'] = addresses
                    else:
                        # Convert simple list to proper format
                        create_params['addresses'] = [{'name': addr} for addr in addresses]
                
                if description:
                    create_params['description'] = description
                    
                if monitor:
                    create_params['monitor'] = monitor
                
                # Create the server
                server = gtm.servers.server.create(**create_params)
                log.info("GTM: GSLB Server {} created successfully".format(server_name))
            
            return server
            
        except Exception as e:
            log.error("GTM: Error creating GSLB server {}: {}".format(server_name, str(e)))
            raise F5CcclError(msg="Error creating GSLB server: {}".format(str(e)))

    def create_virtual_server_on_gslb_server(self, gtm, server_name, vs_name, 
                                            destination, enabled=True, 
                                            translation_address=None, 
                                            translation_port=None, monitor=None):
        """ Create a virtual server on an existing GSLB server
        
        Args:
            gtm: GTM object from BIG-IP management root
            server_name: Name of the GSLB server to add virtual server to
            vs_name: Name of the virtual server to create
            destination: Virtual server destination (format: 'ip:port')
            enabled: Whether the virtual server is enabled (default: True)
            translation_address: Optional translation IP address
            translation_port: Optional translation port
            monitor: Optional health monitor path (e.g., '/Common/http')
            
        Returns:
            virtual_server object if created or loaded successfully
            
        Raises:
            F5CcclError: If virtual server creation fails
        """
        try:
            # Load the server
            server = gtm.servers.server.load(name=server_name)
            
            # Check if virtual server already exists
            vs_exists = server.virtual_servers_s.virtual_server.exists(name=vs_name)
            
            if vs_exists:
                log.debug("GTM: Virtual server {} already exists on server {}".format(
                    vs_name, server_name))
                virtual_server = server.virtual_servers_s.virtual_server.load(name=vs_name)
            else:
                log.info("GTM: Creating virtual server {} on GSLB server {}".format(
                    vs_name, server_name))
                
                # Build creation parameters
                create_params = {
                    'name': vs_name,
                    'destination': destination,
                    'enabled': enabled
                }
                
                if translation_address:
                    create_params['translationAddress'] = translation_address
                    
                if translation_port:
                    create_params['translationPort'] = translation_port
                    
                if monitor:
                    create_params['monitor'] = monitor
                
                # Create the virtual server
                virtual_server = server.virtual_servers_s.virtual_server.create(**create_params)
                log.info("GTM: Virtual server {} created successfully on server {}".format(
                    vs_name, server_name))
            
            return virtual_server
            
        except Exception as e:
            log.error("GTM: Error creating virtual server {} on server {}: {}".format(
                vs_name, server_name, str(e)))
            raise F5CcclError(msg="Error creating virtual server: {}".format(str(e)))

    def ensure_datacenter_exists(self, gtm, datacenter_name, location=None, contact=None):
        """ Validate that GTM datacenter exists
        
        Datacenter must be created manually by the user before deploying GTM configuration.
        This method only validates that it exists.
        
        Args:
            gtm: GTM object from BIG-IP management root
            datacenter_name: Name of the datacenter
            location: Optional location description (unused, kept for compatibility)
            contact: Optional contact information (unused, kept for compatibility)
            
        Returns:
            datacenter object
            
        Raises:
            F5CcclError: If datacenter doesn't exist or loading fails
        """
        try:
            # Check if datacenter exists
            dc_exists = gtm.datacenters.datacenter.exists(name=datacenter_name)
            
            if dc_exists:
                log.debug("GTM: Datacenter {} exists".format(datacenter_name))
                datacenter = gtm.datacenters.datacenter.load(name=datacenter_name)
            else:
                error_msg = "GTM: Datacenter '{}' does not exist. Please create it manually before deploying GTM configuration.".format(datacenter_name)
                log.error(error_msg)
                raise F5CcclError(msg=error_msg)
            
            return datacenter
            
        except F5CcclError:
            raise
        except Exception as e:
            log.error("GTM: Error checking datacenter {}: {}".format(
                datacenter_name, str(e)))
            raise F5CcclError(msg="Error validating datacenter: {}".format(str(e)))

    def _convert_member_to_bigip_reference(self, member_spec, pool_dataserver=None):
        """Convert config member format to BIG-IP member reference format.
        
        Now uses shared _parse_member_spec() helper for single source of truth.
        
        Config formats (auto-detected):
        - New (IPv6): 'DataServer|IP|port' or 'IP|port' (with | separator)
        - Old (IPv4): 'DataServer:IP:port' or 'IP:port' (with : separator, backward compatible)
        
        BIG-IP format:
        - 'server_name:vs_name' (e.g., 'server_10_155_15_101:vs-10-2-0-3-80')
        
        Args:
            member_spec: Member string from config
            pool_dataserver: Optional pool-level DataServer for 'IP|port' or 'IP:port' format
            
        Returns:
            BIG-IP member reference string
        """
        # Use centralized parser (single source of truth)
        dataserver, member_ip, member_port, destination = \
            self._parse_member_spec(member_spec, pool_dataserver)
        
        if dataserver is None:
            log.error("GTM: Cannot convert member '{}' to BIG-IP reference".format(member_spec))
            return member_spec  # Fallback for backward compatibility
        
        # Use centralized formatters
        vs_name = self._format_vs_name(destination)
        server_name = self.format_server_name(dataserver)
        member_ref = "{}:{}".format(server_name, vs_name)
        
        log.debug("GTM: Converted member '{}' to BIG-IP reference '{}'".format(
            member_spec, member_ref))
        return member_ref

    def orchestrate_gtm_infrastructure(self, gtm, partition, gtmConfig, parsed=None):
        """ Orchestrate the creation of GTM infrastructure (datacenter, servers, virtual servers)
        
        This method handles the dependency-aware creation of:
        1. Datacenter (if specified and doesn't exist)
        2. GSLB Servers (one per unique DataServer IP)
        3. Virtual Servers on each GSLB Server (one per pool member)
        
        Args:
            gtm: GTM object from BIG-IP management root
            partition: Partition name
            gtmConfig: GTM configuration dict
            parsed: Optional pre-parsed config data (from _parse_gtm_config_once)
            
        Returns:
            dict with created objects summary
            
        Raises:
            F5CcclError: If critical infrastructure creation fails
        """
        try:
            log.info("GTM: Starting infrastructure orchestration for partition {}".format(partition))
            
            if partition not in gtmConfig:
                log.warning("GTM: Partition {} not found in config".format(partition))
                return {}
            
            # Step 1: Ensure datacenter exists
            datacenter_name = gtmConfig[partition].get("dataCenter", None)
            if not datacenter_name:
                error_msg = "GTM: dataCenter not specified in configuration for partition {}. Please specify a datacenter.".format(partition)
                log.error(error_msg)
                raise F5CcclError(msg=error_msg)
            # Handle full path format (e.g., "/Common/DC-1") or simple name
            if "/" in datacenter_name:
                datacenter_name = datacenter_name.split("/")[-1]
            
            datacenter = self.ensure_datacenter_exists(gtm, datacenter_name)
            
            # Step 2: Get unique DataServer IPs and VS inventory
            # Use pre-parsed data if provided, otherwise parse now
            if parsed is None:
                parsed = self._parse_gtm_config_once(gtmConfig, partition)
            
            dataservers = parsed['dataservers']
            vs_inventory = parsed['vs_inventory']
            
            if not dataservers:
                log.info("GTM: No DataServers found in configuration")
                return {"datacenter": datacenter_name, "servers": 0, "virtual_servers": 0}
            
            log.info("GTM: DataServers to process: {}".format(sorted(dataservers)))
            
            # Step 3: Create GSLB Servers
            created_servers = []
            for dataserver_ip in sorted(dataservers):  # Sort for consistent ordering
                try:
                    # Format server name as server_<ip>
                    server_name = self.format_server_name(dataserver_ip)
                    
                    # Check if server already exists BEFORE attempting to create
                    if gtm.servers.server.exists(name=server_name):
                        log.debug("GTM: Server {} already exists".format(server_name))
                        # Load existing server to verify it's accessible
                        server = gtm.servers.server.load(name=server_name)
                        created_servers.append(server_name)
                    else:
                        # Use product='bigip' - verified to work correctly with SDK partition parameter
                        # Use /Common/gateway_icmp monitor to avoid default /Common/bigip
                        log.info("GTM: Creating new GSLB server {} for DataServer {}".format(
                            server_name, dataserver_ip))
                        server = self.create_gslb_server(
                            gtm=gtm,
                            server_name=server_name,
                            datacenter_name=datacenter_name,
                            addresses=[dataserver_ip],
                            product='bigip',
                            virtual_server_discovery='disabled',
                            monitor='/Common/gateway_icmp'
                        )
                        created_servers.append(server_name)
                except F5CcclError as e:
                    log.error("GTM: Failed to create/load GSLB server for {}: {}".format(
                        dataserver_ip, str(e)))
                    # Continue with other servers
                    continue
            
            log.debug("GTM: Processed {} servers".format(len(created_servers)))
            
            # Step 4: Virtual server inventory already parsed above
            
            # Step 5: Create virtual servers on each GSLB server
            total_vs_created = 0
            for server_name, vs_set in vs_inventory.items():
                if server_name not in created_servers:
                    log.warning("GTM: Server {} was not created, skipping virtual servers".format(
                        server_name))
                    continue
                
                for member_ip, vs_name, destination in vs_set:
                    try:
                        self.create_virtual_server_on_gslb_server(
                            gtm=gtm,
                            server_name=server_name,
                            vs_name=vs_name,
                            destination=destination,
                            enabled=True
                        )
                        total_vs_created += 1
                    except F5CcclError as e:
                        log.error("GTM: Failed to create virtual server {} on {}: {}".format(
                            vs_name, server_name, str(e)))
                        # Continue with other virtual servers
                        continue
            
            summary = {
                "datacenter": datacenter_name,
                "servers": len(created_servers),
                "virtual_servers": total_vs_created
            }
            
            log.info("GTM: Infrastructure orchestration complete: {}".format(summary))
            return summary
            
        except Exception as e:
            log.error("GTM: Critical error during infrastructure orchestration: {}".format(str(e)))
            raise F5CcclError(msg="Infrastructure orchestration failed: {}".format(str(e)))

    def create_HM(self, gtm, partition, monitor, wideIPName):
        """ Create Health Monitor """
        try:
            if bool(monitor):
                if monitor['type'] == "http":
                    exist = gtm.monitor.https.http.exists(
                        name=monitor['name'],
                        partition=partition)
                if monitor['type'] == "https":
                    exist = gtm.monitor.https_s.https.exists(
                        name=monitor['name'],
                        partition=partition)
                if monitor['type'] == "tcp":
                    exist = gtm.monitor.tcps.tcp.exists(
                        name=monitor['name'],
                        partition=partition)
                if not exist:
                    if monitor['type'] == "http":
                        try:
                            gtm.monitor.https.http.create(
                                name=monitor['name'],
                                partition=partition,
                                send=monitor['send'],
                                recv=monitor['recv'],
                                interval=monitor['interval'],
                                timeout=monitor['timeout'])
                        except F5CcclError as e:
                           log.debug("GTM: Error while creating http Health Monitor: %s", e)
                           raise e
                    if monitor['type'] == "https":
                        try:
                            if self.get_bigip_version() >= 16.1:
                                gtm.monitor.https_s.https.create(
                                    name=monitor['name'],
                                    partition=partition,
                                    send=monitor['send'],
                                    recv=monitor['recv'],
                                    sniServerName=wideIPName,
                                    interval=monitor['interval'],
                                    timeout=monitor['timeout'])
                            else:
                                gtm.monitor.https_s.https.create(
                                    name=monitor['name'],
                                    partition=partition,
                                    send=monitor['send'],
                                    recv=monitor['recv'],
                                    interval=monitor['interval'],
                                    timeout=monitor['timeout'])
                        except F5CcclError as e:
                           log.debug("GTM: Error while creating https Health Monitor: %s", e)
                           raise e
                    if monitor['type'] == "tcp":
                        try:
                            gtm.monitor.tcps.tcp.create(
                                name=monitor['name'],
                                partition=partition,
                                interval=monitor['interval'],
                                timeout=monitor['timeout'])
                        except F5CcclError as e:
                           log.debug("GTM: Error while creating tcp Health Monitor: %s", e)
                           raise e
                else:
                    try:
                        if monitor['type'] == "http":
                            obj = gtm.monitor.https.http.load(
                                name=monitor['name'],
                                partition=partition)
                            obj.send = monitor['send']
                            obj.interval = monitor['interval']
                            obj.timeout = monitor['timeout']
                            obj.update()
                            log.debug("GTM: Updated HTTP monitor {}".format(monitor['name']))
                        if monitor['type'] == "https":
                            obj = gtm.monitor.https_s.https.load(
                                name=monitor['name'],
                                partition=partition)
                            obj.send = monitor['send']
                            obj.interval = monitor['interval']
                            obj.timeout = monitor['timeout']
                            if self.get_bigip_version() >= 16.1:
                                obj.sniServerName = wideIPName
                            obj.update()
                            log.debug("GTM: Updated HTTPS monitor {}".format(monitor['name']))
                        if monitor['type'] == "tcp":
                            obj = gtm.monitor.tcps.tcp.load(
                                name=monitor['name'],
                                partition=partition)
                            obj.interval = monitor['interval']
                            obj.timeout = monitor['timeout']
                            obj.update()
                    except F5CcclError as e:
                        log.debug("GTM: Error while Updating Health Monitor: %s", e)
                        raise e
        except F5CcclError as e:
            log.debug("GTM: Error while creating Health Monitor: %s", e)
            raise e


    def remove_member_to_gtm_pool(self,gtm,partition,poolName,memberName):
        """ Remove member to gtm pool 
        
        Args:
            gtm: GTM object
            partition: Partition name
            poolName: Pool name
            memberName: Member reference in BIG-IP format (DataServer:vs_name)
        """
        try:
            # Tenant check for old format only
            # Old format: server:/partition/vs_name
            # New format: DataServer:vs_name (no tenant info in vs_name)
            try:
                parts = memberName.split(":")
                if len(parts) >= 2 and "/" in parts[1]:
                    # Old format - check tenant
                    tenant = parts[1].split("/")[1]
                    if tenant not in self._active_tenants + self._deleted_tenants:
                        log.debug("GTM: Not removing the pool member %s as it may not be created by this CIS instance", memberName)
                        return
                else:
                    # New format - skip tenant check (managed by partition)
                    log.debug("GTM: Removing member {} (new format, no tenant check)".format(memberName))
            except (IndexError, AttributeError):
                # If parsing fails, proceed with deletion (backward compatible)
                log.debug("GTM: Could not parse tenant from member {}, proceeding with removal".format(memberName))
            
            exist=gtm.pools.a_s.a.exists(name=poolName, partition=partition)
            if exist:
                pool = gtm.pools.a_s.a.load(name=poolName,partition=partition)
                
                # Members are stored as server:vs (without /Common/ prefix)
                try:
                    if pool.members_s.member.exists(name=memberName, partition="Common"):
                        memObj = pool.members_s.member.load(name=memberName, partition="Common")
                        memObj.delete()
                        log.info("GTM: Member {} deleted from pool {}".format(memberName, poolName))
                    else:
                        log.warning("GTM: Member {} not found in pool {}".format(memberName, poolName))
                except Exception as e:
                    log.warning("GTM: Error deleting member {}: {}".format(memberName, str(e)))
        except Exception as e:
            log.error("GTM: Error while removing pool member {}: {}".format(memberName, str(e)))
            raise e

    def remove_gtm_pool_to_wideip(self, gtm, wideipName, partition, poolName):
        """ Remove gtm pool to the wideip """
        try:
            wideip = gtm.wideips.a_s.a.load(name=wideipName,partition=partition)
            if wideip.lastResortPool == "":
                wideip.lastResortPool = "none"
            if hasattr(wideip,'pools'):
                for pool in wideip.pools:
                    if pool["name"]==poolName:
                        wideip.pools.remove(pool)
                        wideip.update()
                        log.debug("GTM: Removed pool {} from wideIP".format(poolName))
        except F5CcclError as e:
            log.error("GTM: Error while removing pool: %s", e)
            raise e

    def delete_gtm_pool(self,gtm,partition,wideipName,poolName):
        """ Delete gtm pools """
        try:
            oldConfig = copy.deepcopy(self._gtm_config)
            # Fix this multiple loop 
            if oldConfig[partition]['wideIPs'] is not None:
                for index, wideip in enumerate(oldConfig[partition]['wideIPs']):
                    if wideipName==wideip['name']:
                        for pool_index, pool in enumerate(wideip['pools']):
                            if pool['name']==poolName and pool['members'] is not None:
                                for member in pool['members']:
                                    # Convert config format to BIG-IP member reference
                                    member_ref = self._convert_member_to_bigip_reference(
                                        member, pool.get('DataServer'))
                                    self.remove_member_to_gtm_pool(
                                        gtm,
                                        partition,
                                        poolName,
                                        member_ref)
                                self._gtm_config[partition]['wideIPs'][index]["pools"][pool_index]['members'] = None
                                break
                        break
                obj = gtm.pools.a_s.a.load(
                    name=poolName,
                    partition=partition)
                # delete the gtm pool and remove the pool from wide ip once there are no pool members attached to it.
                if  len(obj.members_s.get_collection()) == 0:
                    self.remove_gtm_pool_to_wideip(gtm,
                        wideipName,partition,poolName)
                    obj.delete()
                    log.info("GTM: Deleted pool {}".format(poolName))
                    self._gtm_config[partition]['wideIPs'][index]["pools"].pop(pool_index)
        except F5CcclError as e:
            log.error("GTM: Error while deleting pool: %s", e)
            raise e


    def delete_gtm_wideip(self,gtm,partition,wideipName):
        """ Delete gtm wideip """
        try:
            oldConfig = copy.deepcopy(self._gtm_config)
            wideip = gtm.wideips.a_s.a.load(
                    name=wideipName,
                    partition=partition)
            if wideip.lastResortPool == "":
                wideip.lastResortPool = "none"
            if hasattr(wideip,'pools'):
                log.debug("GTM: Cannot delete wideIP {} - pools still attached".format(wideipName))
            else:
                wideip.delete()
                log.info("GTM: Deleted wideIP {}".format(wideipName))
                if oldConfig[partition]['wideIPs'] is not None:
                    for index, wideip in enumerate(oldConfig[partition]['wideIPs']):
                        if wideipName == wideip['name']:
                            self._gtm_config[partition]['wideIPs'].pop(index)
        except F5CcclError as e:
            log.error("Could not delete wideip: %s", e)
            raise e

    def delete_gtm_hm_helper(self, partition, monitorName):
        oldConfig = copy.deepcopy(self._gtm_config)
        if oldConfig[partition]['wideIPs'] is not None:
            for index, config in enumerate(oldConfig[partition]['wideIPs']):
                for pool_index, pool in enumerate(config['pools']):
                    if "monitors" in pool.keys():
                        for monitor in pool['monitors']:
                            if monitorName == monitor['name']:
                                return index, pool_index, monitor['type']

    def delete_gtm_hm(self,gtm,partition,monitorName):
        """ Delete gtm health monitor """
        try:
            wideip_index, pool_index, type = self.delete_gtm_hm_helper(partition, monitorName)
            if type=="http":
                obj = gtm.monitor.https.http.load(
                            name=monitorName,
                            partition=partition)
                obj.delete()
                log.info("GTM: Deleted HTTP monitor {}".format(monitorName))
            elif type=="https":
                obj = gtm.monitor.https_s.https.load(
                            name=monitorName,
                            partition=partition)
                obj.delete()
                log.info("GTM: Deleted HTTPS monitor {}".format(monitorName))
            elif type=="tcp":
                obj = gtm.monitor.tcps.tcp.load(
                            name=monitorName,
                            partition=partition)
                obj.delete()
                log.info("GTM: Deleted TCP monitor {}".format(monitorName))
            self._gtm_config[partition]['wideIPs'][wideip_index]["pools"][pool_index].pop("monitor", None)
        except F5CcclError as e:
            log.error("GTM: Could not delete health monitor: %s", e)
            raise e

    def cleanup_unused_virtual_servers(self, gtm, partition, oldConfig=None, newConfig=None, old_parsed=None, new_parsed=None):
        """ Clean up virtual servers that CCCL created but are no longer referenced
        
        Args:
            gtm: GTM object from BIG-IP management root
            partition: Partition name
            oldConfig: Previous configuration (what CCCL created before)
            newConfig: Current configuration (what should exist now)
            old_parsed: Optional pre-parsed old config data (from _parse_gtm_config_once)
            new_parsed: Optional pre-parsed new config data (from _parse_gtm_config_once)
            
        This method:
        1. Gets servers and members from CCCL's previous config (what CCCL created)
        2. Gets servers and members from current config (what should exist)
        3. Deletes virtual servers that CCCL created but are no longer needed
        
        Does NOT delete manually created virtual servers.
        """
        try:
            log.debug("GTM: Starting cleanup of unused virtual servers")
            
            # If configs not provided, use self._gtm_config for both (backwards compatibility)
            if oldConfig is None:
                oldConfig = self._gtm_config
            if newConfig is None:
                newConfig = self._gtm_config
            
            # Get members from OLD config (use pre-parsed if available)
            if old_parsed is not None:
                old_members = old_parsed['all_member_refs']
                old_servers = old_parsed['all_server_names']
            else:
                old_members = set()
                old_servers = set()
                if partition in oldConfig and 'wideIPs' in oldConfig[partition]:
                    for wideip in oldConfig[partition].get('wideIPs', []) or []:
                        for pool in wideip.get('pools', []) or []:
                            for member in pool.get('members', []) or []:
                                member_ref = self._convert_member_to_bigip_reference(
                                    member, pool.get('DataServer'))
                                old_members.add(member_ref)
                                old_servers.add(member_ref.split(':')[0])
            
            # Get members from NEW config (use pre-parsed if available)
            if new_parsed is not None:
                new_members = new_parsed['all_member_refs']
                new_servers = new_parsed['all_server_names']
            else:
                new_members = set()
                new_servers = set()
                if partition in newConfig and 'wideIPs' in newConfig[partition]:
                    for wideip in newConfig[partition].get('wideIPs', []) or []:
                        for pool in wideip.get('pools', []) or []:
                            for member in pool.get('members', []) or []:
                                member_ref = self._convert_member_to_bigip_reference(
                                    member, pool.get('DataServer'))
                                new_members.add(member_ref)
                                new_servers.add(member_ref.split(':')[0])
            
            # Only delete VSs that CCCL created (in old) but are no longer needed (not in new)
            members_to_delete = old_members - new_members
            
            log.debug("GTM: Old members (CCCL created): {}".format(old_members))
            log.debug("GTM: New members (should exist): {}".format(new_members))
            log.debug("GTM: Members to delete: {}".format(members_to_delete))
            
            # Delete the virtual servers for removed members
            for member_ref in members_to_delete:
                try:
                    parts = member_ref.split(':')
                    if len(parts) != 2:
                        continue
                    server_name = parts[0]
                    vs_name = parts[1]
                    
                    if not gtm.servers.server.exists(name=server_name):
                        continue
                    
                    server = gtm.servers.server.load(name=server_name)
                    
                    if server.virtual_servers_s.virtual_server.exists(name=vs_name):
                        log.debug("GTM: Deleting unused virtual server {} from server {}".format(
                            vs_name, server_name))
                        vs = server.virtual_servers_s.virtual_server.load(name=vs_name)
                        vs.delete()
                    
                except Exception as e:
                    log.warning("GTM: Could not delete VS for {}: {}".format(member_ref, str(e)))
                
            log.debug("GTM: Completed cleanup of unused virtual servers")
            
        except Exception as e:
            log.error("GTM: Error during virtual server cleanup: {}".format(str(e)))

    def cleanup_unused_gslb_servers(self, gtm, datacenter_name=None, oldConfig=None, newConfig=None, old_parsed=None, new_parsed=None):
        """ Clean up GSLB servers that CCCL created but have no virtual servers
        
        Args:
            gtm: GTM object from BIG-IP management root
            datacenter_name: Optional datacenter name to limit cleanup scope
            oldConfig: Previous configuration (what CCCL created before)
            newConfig: Current configuration (what should exist now)
            old_parsed: Optional pre-parsed old config data (from _parse_gtm_config_once)
            new_parsed: Optional pre-parsed new config data (from _parse_gtm_config_once)
            
        This method:
        1. Gets servers from CCCL's previous config (what CCCL created)
        2. Gets servers from current config (what should exist)
        3. Deletes servers that CCCL created but have no virtual servers left
        
        Does NOT delete manually created servers.
        """
        try:
            log.info("GTM: Starting cleanup of unused GSLB servers")
            
            # If configs not provided, use self._gtm_config for both (backwards compatibility)
            if oldConfig is None:
                oldConfig = self._gtm_config
            if newConfig is None:
                newConfig = self._gtm_config
            
            # Get servers from OLD config (use pre-parsed if available)
            if old_parsed is not None:
                old_servers = old_parsed['all_server_names']
            else:
                old_servers = set()
                if oldConfig:
                    for partition in oldConfig:
                        if 'wideIPs' in oldConfig[partition]:
                            for wideip in oldConfig[partition].get('wideIPs', []) or []:
                                for pool in wideip.get('pools', []) or []:
                                    for member in pool.get('members', []) or []:
                                        member_ref = self._convert_member_to_bigip_reference(
                                            member, pool.get('DataServer'))
                                        server_name = member_ref.split(':')[0]
                                        old_servers.add(server_name)
            
            # Get servers from NEW config (use pre-parsed if available)
            if new_parsed is not None:
                new_servers = new_parsed['all_server_names']
            else:
                new_servers = set()
                if newConfig:
                    for partition in newConfig:
                        if 'wideIPs' in newConfig[partition]:
                            for wideip in newConfig[partition].get('wideIPs', []) or []:
                                for pool in wideip.get('pools', []) or []:
                                    for member in pool.get('members', []) or []:
                                        member_ref = self._convert_member_to_bigip_reference(
                                            member, pool.get('DataServer'))
                                        server_name = member_ref.split(':')[0]
                                        new_servers.add(server_name)
            
            log.debug("GTM: Old servers (CCCL created): {}".format(old_servers))
            log.debug("GTM: New servers (should exist): {}".format(new_servers))
            
            # Check servers that CCCL created (in old) - delete if they have no VSs
            for server_name in old_servers:
                try:
                    if not gtm.servers.server.exists(name=server_name):
                        log.debug("GTM: Server {} does not exist".format(server_name))
                        continue
                    
                    server = gtm.servers.server.load(name=server_name)
                    
                    # Filter by datacenter if specified
                    if datacenter_name:
                        server_dc = getattr(server, 'datacenter', None)
                        if server_dc:
                            server_dc = server_dc.split('/')[-1]
                        if server_dc != datacenter_name:
                            continue
                    
                    # Check if server has any virtual servers
                    try:
                        vs_count = 0
                        vs_list = server.virtual_servers_s.get_collection()
                        vs_count = len(list(vs_list))
                        
                        if vs_count == 0:
                            # Server has no VSs - check if it's still in new config
                            if server_name in new_servers:
                                log.debug("GTM: Server {} has no VSs but still in config, keeping it".format(server_name))
                            else:
                                log.info("GTM: Deleting unused GSLB server {} (CCCL created, no VSs)".format(server_name))
                                server.delete()
                        else:
                            log.debug("GTM: Server {} has {} virtual servers, keeping it".format(
                                server_name, vs_count))
                    except Exception as e:
                        log.debug("GTM: Could not check VS count for server {}: {}".format(
                            server_name, str(e)))
                    
                except Exception as e:
                    log.warning("GTM: Error processing GSLB server {}: {}".format(
                        server_name, str(e)))
            
            log.info("GTM: Completed cleanup of unused GSLB servers")
            
        except Exception as e:
            log.error("GTM: Error during GSLB server cleanup: {}".format(str(e)))

    def process_config(self, d1, d2):
        """ Process old and new config """
        def _get_resource_from_list(lst, rsc_name):
            for rsc in lst:
                if rsc["name"] == rsc_name:
                    return rsc

        def _are_wip_equal(wip1, wip2):
            if wip1["recordType"] != wip2["recordType"]:
                return False
            if wip1["loadBalancingMode"] != wip2["loadBalancingMode"]:
                return False

            pool_set1 = set([p["name"] for p in wip1["pools"]])
            pool_set2 = set([p["name"] for p in wip2["pools"]])

            new_pools = pool_set2 - pool_set1
            del_pools = pool_set1 - pool_set2

            if len(new_pools) or len(del_pools):
                return False

            return True

        def _are_pools_equal(pool1, pool2):
            if pool1["recordType"] != pool2["recordType"]:
                return False
            if pool1["loadBalancingMode"] != pool2["loadBalancingMode"]:
                return False

            mem_set1 = set(pool1["members"])
            mem_set2 = set(pool2["members"])

            if len(mem_set1) or len(mem_set2):
                return False

            if pool1["monitor"]["name"] != pool2["monitor"]["name"]:
                return False

            return True

        def _get_crud_wide_ips(d1, d2):
            wip_set1 = set([v["name"] for v in _get_value(d1,"wideIPs")])
            wip_set2 = set([v["name"] for v in _get_value(d2,"wideIPs")])

            del_wips = list(wip_set1 - wip_set2)
            new_wips = list(wip_set2 - wip_set1)
            cur_wips = wip_set1.intersection(wip_set2)
            update_wips = []

            for wip_name in cur_wips:
                wip1 = _get_resource_from_list(_get_value(d1,"wideIPs"), wip_name)
                wip2 = _get_resource_from_list(_get_value(d2,"wideIPs"), wip_name)

                if wip1 != wip2:
                    update_wips.append(wip_name)

            return new_wips, del_wips, update_wips

        def _get_crud_pools(d1, d2):
            pools1 = []
            pools2 = []
            for wip in _get_value(d1,"wideIPs"):
                pools1 += wip["pools"]
            for wip in _get_value(d2,"wideIPs"):
                pools2 += wip["pools"]

            pool_set1 = set([p["name"] for p in pools1])
            pool_set2 = set([p["name"] for p in pools2])

            new_pools = list(pool_set2 - pool_set1)
            del_pools = list(pool_set1 - pool_set2)
            cur_pools = pool_set1.intersection(pool_set2)
            update_pools = []

            for pool_name in cur_pools:
                pool1 = _get_resource_from_list(pools1, pool_name)
                pool2 = _get_resource_from_list(pools2, pool_name)

                if pool1 != pool2:
                    update_pools.append(pool_name)

            return new_pools, del_pools, update_pools

        def _get_value(d,k):
            if d[k] is None:
                return dict()
            return d[k]

        def _get_crud_monitors(d1, d2):
            pools1 = []
            pools2 = []
            for wip in _get_value(d1,"wideIPs"):
                pools1 += wip["pools"]
            for wip in _get_value(d2,"wideIPs"):
                pools2 += wip["pools"]

            monitors1, monitors2 = [], []
            for p in pools1:
                if p.get("monitors"):
                    monitors1 += p["monitors"]
            for p in pools2:
                if p.get("monitors"):
                    monitors2 += p["monitors"]

            mon_set1 = set([m["name"] for m in monitors1])
            mon_set2 = set([m["name"] for m in monitors2])

            new_mons = list(mon_set2 - mon_set1)
            del_mons = list(mon_set1 - mon_set2)
            cur_mons = mon_set1.intersection(mon_set2)
            update_mons = []

            for mon_name in cur_mons:
                mon1 = _get_resource_from_list(monitors1, mon_name)
                mon2 = _get_resource_from_list(monitors2, mon_name)

                if mon1 != mon2:
                    update_mons.append(mon_name)

            return new_mons, del_mons, update_mons

        new_wips, del_wips, update_wips = _get_crud_wide_ips(d1, d2)

        new_pools, del_pools, update_pools = _get_crud_pools(d1, d2)

        new_mons, del_mons, update_mons = _get_crud_monitors(d1, d2)

        return {
            "create": {
                "wideIPs": new_wips,
                "pools": new_pools,
                "monitors": new_mons
            },
            "delete": {
                "wideIPs": del_wips,
                "pools": del_pools,
                "monitors": del_mons
            },
            "update": {
                "wideIPs": update_wips,
                "pools": update_pools,
                "monitors": update_mons
            }
        }

    def create_reverse_map(self,d):
        rev_map = dict()
        rev_map["pools"] = dict()
        rev_map["monitors"] = dict()
        if d["wideIPs"] is None:
            di = dict()
        else:
            di = d["wideIPs"]
        for wip in di:
            wip_name = wip["name"]
            for pool in wip["pools"]:
                pool_name = pool["name"]
                try:
                    rev_map["pools"][pool_name].append(wip_name)
                except:
                    rev_map["pools"][pool_name] = [wip_name]

                try:
                    for monitor in pool["monitors"]:
                        rev_map["monitors"][monitor["name"]] = pool_name
                except:
                    pass
        return rev_map

def _parse_config(config_file):
    def _file_exist_cb(log_success):
        if os.path.exists(config_file):
            if log_success:
                log.info('Config file: {} found'.format(config_file))
            return (True, None)
        else:
            return (False, 'Waiting for config file {}'.format(config_file))
    _retry_backoff(_file_exist_cb)

    with open(config_file, 'r') as config:
        fcntl.lockf(config.fileno(), fcntl.LOCK_SH, 0, 0, 0)
        data = config.read()
        fcntl.lockf(config.fileno(), fcntl.LOCK_UN, 0, 0, 0)
        config_json = json.loads(data)
        log.debug('loaded configuration file successfully')
        return config_json


def _handle_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
            '--config-file',
            type=str,
            required=True,
            help='BigIp configuration file')
    parser.add_argument(
        '--ctlr-prefix',
        type=str,
        required=True,
        help='Controller name prefix'
    )
    args = parser.parse_args()

    basename = os.path.basename(args.config_file)
    if not basename or 0 == len(basename):
        raise ConfigError('must provide a file path')

    args.config_file = os.path.realpath(args.config_file)

    return args


def _handle_global_config(config):
    level = DEFAULT_LOG_LEVEL
    verify_interval = DEFAULT_VERIFY_INTERVAL

    if config and 'global' in config:
        global_cfg = config['global']

        if 'log-level' in global_cfg:
            log_level = global_cfg['log-level']
            try:
                level = logging.getLevelName(log_level.upper())
            except (AttributeError):
                log.warn('The "global:log-level" field in the configuration '
                         'file should be a string')

        if 'verify-interval' in global_cfg:
            try:
                verify_interval = float(global_cfg['verify-interval'])
                if verify_interval < 0:
                    verify_interval = DEFAULT_VERIFY_INTERVAL
                    log.warn('The "global:verify-interval" field in the '
                             'configuration file should be a non-negative '
                             'number')
            except (ValueError):
                log.warn('The "global:verify-interval" field in the '
                         'configuration file should be a number')

        vxlan_partition = global_cfg.get('vxlan-partition')

    try:
        root_logger.setLevel(level)
        if level > logging.DEBUG:
            logging.getLogger('requests.packages.urllib3.'
                              'connectionpool').setLevel(logging.WARNING)
    except:
        level = DEFAULT_LOG_LEVEL
        root_logger.setLevel(level)
        if level > logging.DEBUG:
            logging.getLogger('requests.packages.urllib3.'
                              'connectionpool').setLevel(logging.WARNING)
        log.warn('Undefined value specified for the '
                 '"global:log-level" field in the configuration file')

    # level only is needed for unit tests
    return verify_interval, level, vxlan_partition

def get_credentials():
    """
    Unified function to retrieve credentials.
    First tries Unix socket, then falls back to environment variables.
    Returns:
        dict: {'username': '...', 'password': '...'}
    """
    # First check credentials over Unix Socket
    credentials = get_credentials_from_socket()
    if credentials:
        return credentials

    # Check Environment Variables
    credential_sources = tuple()
    if not credentials or not credentials.get("bigip_username", ""):
        credential_sources = credential_sources + (('bigip', get_credentials_from_env),)

    if not credentials or not credentials.get("gtm_username", ""):
        credential_sources = credential_sources + (('gtm', get_gtm_credentials_from_env),)

    credentials = {}
    for prefix, fetch_func in credential_sources:
        env_credentials = fetch_func()
        if env_credentials:
            username, password = env_credentials
            credentials[f'{prefix}_username'] = username
            credentials[f'{prefix}_password'] = password

    if not credentials.get("gtm_username", ""):
        credentials["gtm_username"] = credentials["bigip_username"]
    if not credentials.get("gtm_password", ""):
        credentials["gtm_password"] = credentials["bigip_password"]

    return credentials


def get_credentials_from_env():
    """
    Retrieve credentials from environment variables.
    Returns:
        tuple: (username, password) if found, else None.
    """
    log.debug("Checking for credentials in environment variables...")
    username = os.getenv("BIGIP_USERNAME")
    password = os.getenv("BIGIP_PASSWORD")

    if username and password:
        log.info("successfully fetched BIGIP credentials from environment variables.")
        return username, password
    else:
        log.error("Failed to get BIGIP credentials from environment variables.")
        return None

def get_gtm_credentials_from_env():
    """
    Retrieve credentials from environment variables.
    Returns:
        tuple: (username, password) if found, else None.
    """
    log.debug("Checking for GTM credentials in environment variables...")
    username = os.getenv("GTM_BIGIP_USERNAME")
    password = os.getenv("GTM_BIGIP_PASSWORD")

    if username and password:
        log.info("successfully fetched GTM credentials from environment variables.")
        return username, password
    else:
        log.error("Failed to get GTM credentials from environment variables.")
        return None

def get_credentials_from_socket():
    socket_path = "/tmp/secure_cis.sock"
    client = None

    if not os.path.exists(socket_path):
        log.error(f"Socket file not found: {socket_path}")
        return None
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(socket_path)
        log.info("Connected to server.")

        data = client.recv(4096).decode('utf-8')
        credentials = json.loads(data)
        if credentials:
            if credentials.get('bigip_username', '') != "" and credentials.get('bigip_password', '') != "":
                log.info("successfully fetched BIGIP credentials from socket.")
            if credentials.get('gtm_username', '') != "" and credentials.get('gtm_password', '') != "":
                log.info("successfully fetched GTM credentials from socket.")
        return credentials

    except ConnectionError as e:
        log.error(f"Connection failed: {e}")
    finally:
        client.close()



def _handle_bigip_config(config):
    if (not config) or ('bigip' not in config):
        raise ConfigError('Configuration file missing "bigip" section')
    bigip = config['bigip']
    if 'url' not in bigip:
        raise ConfigError('Configuration file missing "bigip:url" section')
    if ('partitions' not in bigip) or (len(bigip['partitions']) == 0):
        raise ConfigError('Configuration file must specify at least one '
                          'partition in the "bigip:partitions" section')

    if 'username' not in config['bigip']:
        raise ConfigError('missing config '
                          '"bigip:username" section')
    if 'password' not in config['bigip']:
        raise ConfigError('missing config '
                          '"bigip:password" section')

    url = urlparse(bigip['url'])
    host = url.hostname
    port = url.port
    if not port:
        port = 443

    return host, port

def _handle_credentials(config):
    credentials = get_credentials()
    if credentials:
        config['bigip']['username'] = credentials.get('bigip_username', '')
        config['bigip']['password'] = credentials.get('bigip_password', '')
        if 'gtm_bigip' in config:
            config['gtm_bigip']['username'] = credentials.get('gtm_username', '')
            config['gtm_bigip']['password'] = credentials.get('gtm_password', '')
    else:
        log.error("Failed to retrieve credentials.")
    return config


def _handle_vxlan_config(config):
    if config and 'vxlan-fdb' in config:
        fdb = config['vxlan-fdb']
        if 'name' not in fdb:
            raise ConfigError('Configuration file missing '
                              '"vxlan-fdb:name" section')
        if 'records' not in fdb:
            raise ConfigError('Configuration file missing '
                              '"vxlan-fdb:records" section')
    if config and 'vxlan-arp' in config:
        arp = config['vxlan-arp']
        if 'arps' not in arp:
            raise ConfigError('Configuration file missing '
                              '"vxlan-arp:arps" section')

    if config and 'static-routes' in config:
        route = config['static-routes']
        if 'routes' not in route:
            raise ConfigError('Configuration file missing '
                              '"static-routes:routes" section')


def _set_user_agent(prefix):
    try:
        with open('/app/vendor/src/f5/VERSION_BUILD.json', 'r') \
                as version_file:
            data = json.load(version_file)
            user_agent = \
                prefix + "-bigip-ctlr-" + data['version'] + '-' + data['build']
    except Exception as e:
        user_agent = prefix + "-bigip-ctlr-VERSION-UNKNOWN"
        log.error("Could not read version file: %s", e)

    return user_agent


def _retry_backoff(cb):
    RETRY_INTERVAL = 1
    log_interval = 0.5
    elapsed = 0.5
    log_success = False
    while 1:
        if log_interval > 0.5:
            log_success = True
        (success, val) = cb(log_success)
        if success:
            return val
        if elapsed == log_interval:
            elapsed = 0
            log_interval *= 2
            log.error("Encountered error: {}. Retrying for {} seconds.".format(
                val, int(log_interval)
            ))
        time.sleep(RETRY_INTERVAL)
        elapsed += RETRY_INTERVAL


def _find_net_schema():
    paths = [path for path in sys.path if 'site-packages' in path]
    for path in paths:
        for root, dirs, files in os.walk(path):
            if NET_SCHEMA_NAME in files:
                return os.path.join(root, NET_SCHEMA_NAME)
    for root, dirs, files in os.walk('/app/src/f5-cccl'):
        if NET_SCHEMA_NAME in files:
            return os.path.join(root, NET_SCHEMA_NAME)
    log.info('Could not find CCCL schema: {}'.format(NET_SCHEMA_NAME))
    return ''


def _is_ltm_disabled(config):
    try:
        return config['global']['disable-ltm']
    except KeyError:
        return False

def _is_arp_disabled(config):
    try:
        return config['global']['disable-arp']
    except KeyError:
        return False

def _is_gtm_config(config):
    try:
        return config['global']['gtm']
    except KeyError:
        return False

def _is_static_routing_enabled(config):
    try:
        return config['global']['static-route-mode']
    except KeyError:
        return False

def _is_cis_secondary(config):
    try:
        return config['global']['multi-cluster-mode'] == "secondary"
    except KeyError:
        return False

def _is_cis_in_arbitrator_mode(config):
    try:
        return config['global']['multi-cluster-mode'] == "arbitrator"
    except KeyError:
        return False

def _is_leader(config):
    try:
        return config['is-leader']
    except KeyError:
        return False
    
def _is_primary_cluster_status_up(config):
    try:
        return config['primary-cluster-status']
    except KeyError:
        return False
def main():
    try:
        args = _handle_args()

        config = _parse_config(args.config_file)
        verify_interval, _, vxlan_partition = _handle_global_config(config)
        config = _handle_credentials(config)
        host, port = _handle_bigip_config(config)

        # FIXME (kenr): Big-IP settings are currently static (we ignore any
        #               changes to these fields in subsequent updates). We
        #               may want to make the changes dynamic in the future.

        # BIG-IP to manage
        def _bigip_connect_cb(log_success):
            try:
                bigip = mgmt_root(
                    host,
                    config['bigip']['username'],
                    config['bigip']['password'],
                    port,
                    "tmos")
                if log_success:
                    log.info('BIG-IP connection established.')
                return (True, bigip)
            except Exception as e:
                return (False, 'BIG-IP connection error: {}'.format(e))
        bigip = _retry_backoff(_bigip_connect_cb)

        # Read version and build info, set user-agent for ICR session
        user_agent = _set_user_agent(args.ctlr_prefix)

        # GTM BIG-IP to manage
        def _gtmbigip_connect_cb(log_success):
            url = urlparse(config['gtm_bigip']['url'])
            host = url.hostname
            port = url.port
            if not port:
                port = 443
            try:
                bigip = mgmt_root(
                    host,
                    config['gtm_bigip']['username'],
                    config['gtm_bigip']['password'],
                    port,
                    "tmos")
                if log_success:
                    log.info('GTM BIG-IP connection established.')
                return (True, bigip)
            except Exception as e:
                return (False, 'GTM BIG-IP connection error: {}'.format(e))

        managers = []
        if not _is_ltm_disabled(config):
            for partition in config['bigip']['partitions']:
                # Management for the BIG-IP partitions
                manager = CloudServiceManager(
                    bigip,
                    partition,
                    user_agent=user_agent)
                managers.append(manager)
        if vxlan_partition:
            # Management for net resources (VXLAN)
            manager = CloudServiceManager(
                bigip,
                vxlan_partition,
                user_agent=user_agent,
                prefix=args.ctlr_prefix,
                schema_path=_find_net_schema())
            managers.append(manager)
        if _is_gtm_config(config):
            if "gtm_bigip" in config:
                gtmbigip = _retry_backoff(_gtmbigip_connect_cb)
            else:
                gtmbigip = _retry_backoff(_bigip_connect_cb)
                log.info("GTM: Missing gtm_bigip section on config.")
            for partition in config['bigip']['partitions']:
                # Management for the BIG-IP partitions
                manager = CloudServiceManager(
                    gtmbigip,
                    partition,
                    user_agent=user_agent,
                    gtm=True)
                managers.append(manager)

        handler = ConfigHandler(args.config_file,
                                managers,
                                verify_interval)

        if os.path.exists(args.config_file):
            handler.notify_reset()

        watcher = ConfigWatcher(args.config_file, handler.notify_reset)
        watcher.loop()
        handler.stop()
    except (IOError, ValueError, ConfigError) as e:
        log.error(e)
        sys.exit(1)
    except Exception as e:
        log.exception(f'Unexpected error: {str(e)}')
        sys.exit(1)

    return 0


if __name__ == "__main__":
    main()

