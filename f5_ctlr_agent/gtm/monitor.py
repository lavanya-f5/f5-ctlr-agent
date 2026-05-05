"""GTM Health Monitor Management Module.

This module handles GTM health monitor creation, updates, and deletion.
Supports HTTP, HTTPS, and TCP monitor types with BIG-IP version-specific features.
"""

import logging
from f5_cccl.exceptions import F5CcclError

log = logging.getLogger(__name__)


class GTMMonitor:
    """Manages GTM health monitor lifecycle operations.
    
    This class provides methods for creating, updating, and deleting health monitors
    for GTM pools. Supports HTTP, HTTPS, and TCP monitor types with automatic
    version detection for advanced features like SNI.
    
    Attributes:
        gtm: F5 SDK GTM object
        partition: BIG-IP partition name
        bigip_version: BIG-IP version (cached after first fetch)
    """

    def __init__(self, gtm, partition, bigip_version_getter=None):
        """Initialize GTM monitor manager.
        
        Args:
            gtm: F5 SDK GTM object for API operations
            partition: BIG-IP partition name
            bigip_version_getter: Optional callable that returns BIG-IP version as float
                                 (e.g., 16.1, 15.1). If None, version detection is skipped.
        """
        self.gtm = gtm
        self.partition = partition
        self._bigip_version_getter = bigip_version_getter
        self._cached_version = None

    def _get_bigip_version(self):
        """Get BIG-IP version with caching.
        
        Returns:
            float: BIG-IP version (e.g., 16.1, 15.1) or None if no version getter
        """
        if self._bigip_version_getter is None:
            return None
        
        if self._cached_version is None:
            self._cached_version = self._bigip_version_getter()
        
        return self._cached_version

    def create_monitor(self, monitor, wideip_name):
        """Create or update health monitor.
        
        Args:
            monitor: Monitor configuration dict with keys:
                    - name: Monitor name
                    - type: "http", "https", or "tcp"
                    - send: Send string (HTTP/HTTPS only)
                    - recv: Receive string (HTTP/HTTPS only)
                    - interval: Check interval in seconds
                    - timeout: Timeout in seconds
            wideip_name: Parent wideIP name (used for SNI on BIG-IP 16.1+)
        
        Raises:
            F5CcclError: On monitor creation or update failure
        """
        try:
            if not bool(monitor):
                return

            monitor_type = monitor['type']
            monitor_name = monitor['name']
            
            # Check existence based on type
            if monitor_type == "http":
                exist = self.gtm.monitor.https.http.exists(
                    name=monitor_name,
                    partition=self.partition)
            elif monitor_type == "https":
                exist = self.gtm.monitor.https_s.https.exists(
                    name=monitor_name,
                    partition=self.partition)
            elif monitor_type == "tcp":
                exist = self.gtm.monitor.tcps.tcp.exists(
                    name=monitor_name,
                    partition=self.partition)
            else:
                log.error("GTM: Unsupported monitor type: {}".format(monitor_type))
                raise F5CcclError(msg="Unsupported monitor type: {}".format(monitor_type))

            if not exist:
                # Create new monitor
                self._create_new_monitor(monitor, monitor_type, wideip_name)
            else:
                # Update existing monitor
                self._update_existing_monitor(monitor, monitor_type, wideip_name)

        except F5CcclError as e:
            log.debug("GTM: Error while creating Health Monitor: %s", e)
            raise e

    def _create_new_monitor(self, monitor, monitor_type, wideip_name):
        """Create new health monitor.
        
        Args:
            monitor: Monitor configuration dict
            monitor_type: "http", "https", or "tcp"
            wideip_name: Parent wideIP name (for SNI)
        
        Raises:
            F5CcclError: On creation failure
        """
        try:
            if monitor_type == "http":
                self.gtm.monitor.https.http.create(
                    name=monitor['name'],
                    partition=self.partition,
                    send=monitor['send'],
                    recv=monitor['recv'],
                    interval=monitor['interval'],
                    timeout=monitor['timeout'])
                log.info("GTM: Created HTTP monitor {}".format(monitor['name']))
                
            elif monitor_type == "https":
                bigip_version = self._get_bigip_version()
                if bigip_version is not None and bigip_version >= 16.1:
                    # BIG-IP 16.1+ supports SNI
                    self.gtm.monitor.https_s.https.create(
                        name=monitor['name'],
                        partition=self.partition,
                        send=monitor['send'],
                        recv=monitor['recv'],
                        sniServerName=wideip_name,
                        interval=monitor['interval'],
                        timeout=monitor['timeout'])
                    log.info("GTM: Created HTTPS monitor {} with SNI".format(monitor['name']))
                else:
                    # Pre-16.1 or no version info
                    self.gtm.monitor.https_s.https.create(
                        name=monitor['name'],
                        partition=self.partition,
                        send=monitor['send'],
                        recv=monitor['recv'],
                        interval=monitor['interval'],
                        timeout=monitor['timeout'])
                    log.info("GTM: Created HTTPS monitor {}".format(monitor['name']))
                    
            elif monitor_type == "tcp":
                self.gtm.monitor.tcps.tcp.create(
                    name=monitor['name'],
                    partition=self.partition,
                    interval=monitor['interval'],
                    timeout=monitor['timeout'])
                log.info("GTM: Created TCP monitor {}".format(monitor['name']))
                
        except F5CcclError as e:
            log.debug("GTM: Error while creating {} Health Monitor: {}".format(
                monitor_type, e))
            raise e

    def _update_existing_monitor(self, monitor, monitor_type, wideip_name):
        """Update existing health monitor.
        
        Args:
            monitor: Monitor configuration dict
            monitor_type: "http", "https", or "tcp"
            wideip_name: Parent wideIP name (for SNI)
        
        Raises:
            F5CcclError: On update failure
        """
        try:
            if monitor_type == "http":
                obj = self.gtm.monitor.https.http.load(
                    name=monitor['name'],
                    partition=self.partition)
                obj.send = monitor['send']
                obj.recv = monitor['recv']
                obj.interval = monitor['interval']
                obj.timeout = monitor['timeout']
                obj.update()
                log.debug("GTM: Updated HTTP monitor {}".format(monitor['name']))
                
            elif monitor_type == "https":
                obj = self.gtm.monitor.https_s.https.load(
                    name=monitor['name'],
                    partition=self.partition)
                obj.send = monitor['send']
                obj.recv = monitor['recv']
                obj.interval = monitor['interval']
                obj.timeout = monitor['timeout']
                bigip_version = self._get_bigip_version()
                if bigip_version is not None and bigip_version >= 16.1:
                    obj.sniServerName = wideip_name
                obj.update()
                log.debug("GTM: Updated HTTPS monitor {}".format(monitor['name']))
                
            elif monitor_type == "tcp":
                obj = self.gtm.monitor.tcps.tcp.load(
                    name=monitor['name'],
                    partition=self.partition)
                obj.interval = monitor['interval']
                obj.timeout = monitor['timeout']
                obj.update()
                log.debug("GTM: Updated TCP monitor {}".format(monitor['name']))
                
        except F5CcclError as e:
            log.debug("GTM: Error while updating {} Health Monitor: {}".format(
                monitor_type, e))
            raise e

    def delete_monitor(self, monitor_name, monitor_type):
        """Delete health monitor.
        
        Args:
            monitor_name: Name of monitor to delete
            monitor_type: "http", "https", or "tcp"
        
        Raises:
            F5CcclError: On transient deletion errors (permanent errors logged only)
        """
        try:
            if monitor_type == "http":
                obj = self.gtm.monitor.https.http.load(
                    name=monitor_name,
                    partition=self.partition)
                obj.delete()
                log.info("GTM: Deleted HTTP monitor {}".format(monitor_name))
                
            elif monitor_type == "https":
                obj = self.gtm.monitor.https_s.https.load(
                    name=monitor_name,
                    partition=self.partition)
                obj.delete()
                log.info("GTM: Deleted HTTPS monitor {}".format(monitor_name))
                
            elif monitor_type == "tcp":
                obj = self.gtm.monitor.tcps.tcp.load(
                    name=monitor_name,
                    partition=self.partition)
                obj.delete()
                log.info("GTM: Deleted TCP monitor {}".format(monitor_name))
                
        except Exception as e:
            error_str = str(e).lower()
            if '404' in error_str or 'not found' in error_str:
                log.info("GTM: Monitor {} already deleted (404)".format(monitor_name))
            else:
                # Re-raise for caller to handle
                raise
