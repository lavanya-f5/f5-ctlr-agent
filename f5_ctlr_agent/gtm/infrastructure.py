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

"""
GTM infrastructure management for servers and virtual servers.

This module handles the lifecycle of GSLB servers and virtual servers,
including creation, updates, and cleanup. This is the primary target
for parallel execution optimization.
"""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from f5_cccl.exceptions import F5CcclError
from f5_ctlr_agent.gtm.utils import GTMUtils

log = logging.getLogger(__name__)


class GTMInfrastructure:
    """Manages GTM infrastructure (servers and virtual servers).
    
    This class handles:
    - GSLB server creation and management
    - Virtual server creation on GSLB servers
    - Datacenter validation
    - Infrastructure orchestration (sequential and parallel modes)
    - Cleanup of unused servers and virtual servers
    
    Performance Modes:
    - Parallel (DEFAULT): 5-10x faster, ~10-60 sec for large deployments
    - Sequential: Safe fallback, ~1-10 min for large deployments
    
    Parallel execution is ENABLED BY DEFAULT for optimal performance.
    
    Disable parallel execution (if needed):
        1. Set environment variable: GTM_PARALLEL_EXECUTION=false
        2. Or pass enable_parallel=False to constructor
    
    Tune parallel workers (to avoid overwhelming BIG-IP):
        - GTM_SERVER_WORKERS=5 (default: 5, max recommended: 10)
        - GTM_VS_WORKERS=8 (default: 8, max recommended: 12)
        
    Note: Higher worker counts (>10) can overwhelm BIG-IP REST API causing
          502 errors and connection failures. Start conservative and tune up.
    """
    
    def __init__(self, gtm, partition, mgmt_root=None, enable_parallel=None, 
                 server_workers=5, vs_workers=8):
        """Initialize GTM Infrastructure manager.
        
        Args:
            gtm: BIG-IP GTM object from management root
            partition (str): Partition to manage
            mgmt_root: Management root (optional, for version checking)
            enable_parallel (bool): Enable parallel execution (default: True, override with False or GTM_PARALLEL_EXECUTION=false)
            server_workers (int): Parallel threads for servers (default: 5, from env GTM_SERVER_WORKERS)
            vs_workers (int): Parallel threads for VSs (default: 8, from env GTM_VS_WORKERS)
        """
        self._gtm = gtm
        self._partition = partition
        self._mgmt_root = mgmt_root
        
        # Performance configuration - PARALLEL IS DEFAULT
        self._enable_parallel = (
            enable_parallel 
            if enable_parallel is not None 
            else os.getenv('GTM_PARALLEL_EXECUTION', 'true').lower() == 'true'
        )
        self._server_workers = int(os.getenv('GTM_SERVER_WORKERS', str(server_workers)))
        self._vs_workers = int(os.getenv('GTM_VS_WORKERS', str(vs_workers)))
        
        # Thread safety and cleanup tracking
        self._active_executor = None  # Track active ThreadPoolExecutor
        self._executor_lock = threading.Lock()  # Protect executor state
        self._operation_timeout = int(os.getenv('GTM_OPERATION_TIMEOUT', '300'))  # 5 min default
        
        # Rate limiting and retry configuration
        self._task_submission_delay = float(os.getenv('GTM_TASK_DELAY', '0.02'))  # 20ms delay between task submissions
        self._max_retries = int(os.getenv('GTM_MAX_RETRIES', '3'))
        self._retry_backoff_base = float(os.getenv('GTM_RETRY_BACKOFF', '2.0'))  # Exponential backoff base
        
        # FIX: Align urllib3 connection pool size with worker count
        # Default pool size is 10, but we need pool_size >= worker_count to avoid connection discards
        if self._enable_parallel and mgmt_root:
            try:
                import requests
                pool_size = max(self._server_workers, self._vs_workers) + 5
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=pool_size,
                    pool_maxsize=pool_size,
                    max_retries=0  # We handle retries ourselves
                )
                # Apply to iControl REST session - try multiple paths
                session = None
                if hasattr(mgmt_root, 'icrs') and hasattr(mgmt_root.icrs, '_session'):
                    session = mgmt_root.icrs._session  # F5 SDK path
                elif hasattr(mgmt_root, 'icontrol'):
                    session = mgmt_root.icontrol.session
                
                if session:
                    session.mount('https://', adapter)
                    log.info("GTM: Configured urllib3 connection pool: size={}".format(pool_size))
                else:
                    log.warning("GTM: Could not locate iControl session for pool configuration")
            except Exception as e:
                log.warning("GTM: Could not configure connection pool size: {}".format(e))
        
        if self._enable_parallel:
            log.info("GTM: Parallel execution ENABLED (DEFAULT) - server_workers={}, vs_workers={}, pool_size={}, task_delay={}ms".format(
                self._server_workers, self._vs_workers, 
                max(self._server_workers, self._vs_workers) + 5,
                int(self._task_submission_delay * 1000)))
        else:
            log.warning("GTM: Parallel execution DISABLED (sequential mode) - Performance will be slower")
    
    def create_gslb_server(self, server_name, datacenter_name, addresses,
                          product='generic-host', virtual_server_discovery='disabled',
                          description=None, monitor=None):
        """Create GTM GSLB server.
        
        Args:
            server_name (str): Name of the GSLB server
            datacenter_name (str): Datacenter to place server in
            addresses (list): List of IP addresses or address dicts
            product (str): Server product type (default: 'generic-host')
            virtual_server_discovery (str): VS discovery mode (default: 'disabled')
            description (str, optional): Server description
            monitor (str, optional): Health monitor path
            
        Returns:
            Server object from BIG-IP
            
        Note:
            Uses idempotent create - returns existing server if already present.
        """
        try:
            server_exists = self._gtm.servers.server.exists(name=server_name)

            if server_exists:
                log.info("GTM: GSLB Server {} already exists".format(server_name))
                server = self._gtm.servers.server.load(name=server_name)
            else:
                log.info("GTM: Creating GSLB server {}".format(server_name))

                create_params = {
                    'name': server_name,
                    'datacenter': datacenter_name,
                    'product': product,
                    'virtualServerDiscovery': virtual_server_discovery
                }

                if addresses:
                    if isinstance(addresses[0], dict):
                        create_params['addresses'] = addresses
                    else:
                        create_params['addresses'] = [{'name': addr} for addr in addresses]

                if description:
                    create_params['description'] = description
                if monitor:
                    create_params['monitor'] = monitor

                server = self._gtm.servers.server.create(**create_params)
                log.info("GTM: GSLB Server {} created successfully".format(server_name))

            return server

        except Exception as e:
            log.error("GTM: Error creating GSLB server {}: {}".format(server_name, str(e)))
            raise F5CcclError(msg="Error creating GSLB server: {}".format(str(e)))
    
    def create_virtual_server_on_gslb_server(self, server_name, vs_name,
                                            destination, enabled=True,
                                            translation_address=None,
                                            translation_port=None, monitor=None,
                                            server_obj=None):
        """Create a virtual server on an existing GSLB server.
        
        Args:
            server_name (str): Name of the GSLB server
            vs_name (str): Name for the virtual server
            destination (str): Destination IP:PORT
            enabled (bool): Whether VS is enabled (default: True)
            translation_address (str, optional): Translation address
            translation_port (int, optional): Translation port
            monitor (str, optional): Health monitor path
            server_obj: Pre-loaded server object (optimization)
            
        Returns:
            Virtual server object from BIG-IP
            
        Note:
            PERF OPTIMIZATION: Accepts pre-loaded server_obj to avoid
            redundant server.load() calls in batch scenarios.
        """
        try:
            # PERF FIX #1: Use pre-loaded server object if available
            if server_obj is None:
                server_obj = self._gtm.servers.server.load(name=server_name)

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

            # PERF FIX #2: Use try/create instead of exists+load
            try:
                virtual_server = server_obj.virtual_servers_s.virtual_server.create(**create_params)
                log.debug("GTM: Virtual server {} created successfully on server {}".format(
                    vs_name, server_name))
            except Exception as e:
                if "already exists" in str(e).lower():
                    log.debug("GTM: Virtual server {} already exists on server {}".format(
                        vs_name, server_name))
                    virtual_server = server_obj.virtual_servers_s.virtual_server.load(name=vs_name)
                else:
                    log.error("GTM: Failed to create virtual server {} on server {}: {}".format(
                        vs_name, server_name, str(e)))
                    raise

            return virtual_server

        except Exception as e:
            log.error("GTM: Error creating virtual server {} on server {}: {}".format(
                vs_name, server_name, str(e)))
            raise F5CcclError(msg="Error creating virtual server: {}".format(str(e)))
    
    def ensure_datacenter_exists(self, datacenter_name, location=None, contact=None):
        """Validate that GTM datacenter exists.
        
        Args:
            datacenter_name (str): Name of the datacenter
            location (str, optional): Datacenter location
            contact (str, optional): Contact information
            
        Returns:
            Datacenter object from BIG-IP
            
        Raises:
            F5CcclError: If datacenter doesn't exist
            
        Note:
            Datacenters must be created manually beforehand. This method
            only validates existence, it does not create datacenters.
        """
        try:
            dc_exists = self._gtm.datacenters.datacenter.exists(name=datacenter_name)

            if dc_exists:
                log.debug("GTM: Datacenter {} exists".format(datacenter_name))
                datacenter = self._gtm.datacenters.datacenter.load(name=datacenter_name)
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
    
    def orchestrate_with_snapshot(self, gtmConfig, parsed, snapshot):
        """Orchestrate infrastructure using snapshot to skip existing resources.
        
        Automatically uses parallel execution if enabled via configuration.
        
        This is the main orchestration method that creates servers and virtual
        servers. It uses a snapshot of existing BIG-IP state to skip resources
        that already exist, significantly reducing deployment time.
        
        Args:
            gtmConfig (dict): Full GTM configuration
            parsed (dict): Pre-parsed config data from GTMUtils.parse_gtm_config_once()
            snapshot (dict): BIG-IP state snapshot
            
        Returns:
            dict: Summary of orchestration results with keys:
                  - datacenter: Datacenter name
                  - servers: Number of servers managed
                  - virtual_servers: Number of VSs created/managed
                  
        Note:
            If parallel execution is enabled (via GTM_PARALLEL_EXECUTION=true),
            this method automatically delegates to orchestrate_with_snapshot_parallel().
        """
        # Auto-dispatch to parallel mode if enabled
        if self._enable_parallel:
            log.debug("GTM: Auto-dispatching to parallel orchestration mode")
            return self.orchestrate_with_snapshot_parallel(
                gtmConfig, parsed, snapshot, 
                server_workers=self._server_workers,
                vs_workers=self._vs_workers)
        
        # Sequential mode (original implementation)
        try:
            datacenter_name = gtmConfig[self._partition].get("dataCenter", None)
            if not datacenter_name:
                raise F5CcclError(msg="GTM: dataCenter not specified for partition {}".format(self._partition))
            if "/" in datacenter_name:
                datacenter_name = datacenter_name.split("/")[-1]

            self.ensure_datacenter_exists(datacenter_name)

            dataservers = parsed['dataservers']
            vs_inventory = parsed['vs_inventory']

            if not dataservers:
                return {"datacenter": datacenter_name, "servers": 0, "virtual_servers": 0}

            log.info("GTM: DataServers to process: {}".format(sorted(dataservers)))

            # Create only MISSING servers (use snapshot for existence check)
            created_server_objects = {}
            servers_created = 0
            servers_skipped = 0

            for dataserver_ip in sorted(dataservers):
                server_name = GTMUtils.format_server_name(dataserver_ip)

                if server_name in snapshot['servers']:
                    # Server exists — load object for VS creation
                    log.debug("GTM: Server {} exists (from snapshot)".format(server_name))
                    created_server_objects[server_name] = self._gtm.servers.server.load(name=server_name)
                    servers_skipped += 1
                else:
                    # Server doesn't exist — create it
                    server = self.create_gslb_server(
                        server_name=server_name,
                        datacenter_name=datacenter_name,
                        addresses=[dataserver_ip],
                        product='bigip',
                        virtual_server_discovery='disabled',
                        monitor='/Common/gateway_icmp')
                    created_server_objects[server_name] = server
                    servers_created += 1

            log.info("GTM: [SNAPSHOT] Servers: {} created, {} skipped (already exist)".format(
                servers_created, servers_skipped))

            # Create only MISSING VSs — lazy load VS names per server
            total_vs_created = 0
            total_vs_skipped = 0

            for server_name, vs_set in vs_inventory.items():
                if server_name not in created_server_objects:
                    continue

                server_obj = created_server_objects[server_name]

                # Lazy VS load: only fetch when we actually need to check
                existing_vs = snapshot['server_vs'].get(server_name, set())
                if not existing_vs:
                    try:
                        existing_vs = {vs.name for vs in server_obj.virtual_servers_s.get_collection()}
                        snapshot['server_vs'][server_name] = existing_vs
                        log.info("GTM: [SNAPSHOT] Lazy-loaded {} VSs for server {}".format(
                            len(existing_vs), server_name))
                    except Exception as e:
                        # CRITICAL: Distinguish transient vs permanent errors
                        # - Transient error on server with 50 VSs → raising is CHEAPER than 100 create calls
                        # - Permanent error (new server, no VSs) → empty set is correct
                        if GTMUtils.is_transient_error(e):
                            log.warning("GTM: [SNAPSHOT] Transient error fetching VSs for server {}, "
                                       "raising to avoid issue in subsequent operations: {}".format(
                                server_name, str(e)))
                            raise F5CcclError(
                                msg="VS fetch failed for {}: {}".format(server_name, str(e)))
                        else:
                            log.debug("GTM: [SNAPSHOT] No existing VSs for server {} (permanent error): {}".format(
                                server_name, str(e)))
                            existing_vs = set()

                for member_ip, vs_name, destination in vs_set:
                    if vs_name in existing_vs:
                        total_vs_skipped += 1
                        continue
                    try:
                        self.create_virtual_server_on_gslb_server(
                            server_name=server_name,
                            vs_name=vs_name,
                            destination=destination,
                            enabled=True,
                            server_obj=server_obj)
                        total_vs_created += 1
                    except F5CcclError as e:
                        # Log error but continue - matches parallel behavior
                        log.error("GTM: [SEQUENTIAL] Error creating VS {} on server {}: {}".format(
                            vs_name, server_name, str(e)))
                        continue

            log.info("GTM: [SNAPSHOT] VSs: {} created, {} skipped".format(
                total_vs_created, total_vs_skipped))

            return {
                "datacenter": datacenter_name,
                "servers": len(created_server_objects),
                "virtual_servers": total_vs_created + total_vs_skipped
            }

        except Exception as e:
            log.error("GTM: Critical error during infrastructure orchestration: {}".format(str(e)))
            raise F5CcclError(msg="Infrastructure orchestration failed: {}".format(str(e)))
    
    # ========================================================================
    # THREAD CLEANUP AND CANCELLATION METHODS
    # ========================================================================
    
    def shutdown_active_operations(self, wait=True, timeout=30):
        """Shutdown any active parallel operations.
        
        This method should be called when:
        - Configuration changes are detected
        - Controller is shutting down
        - Emergency stop is required
        
        Args:
            wait (bool): Whether to wait for running tasks to complete
            timeout (int): Maximum seconds to wait for shutdown
            
        Returns:
            bool: True if shutdown completed, False if timeout
            
        Note:
            This is safe to call multiple times or when no operations are active.
            Configuration changes will wait for this to complete before starting new operations.
        """
        with self._executor_lock:
            if self._active_executor is None:
                log.debug("GTM: No active parallel operations to shutdown")
                return True
            
            executor = self._active_executor
            log.warning("GTM: Shutting down active parallel operations (wait={}, timeout={}s)".format(
                wait, timeout))
        
        try:
            # Shutdown will stop accepting new tasks and optionally wait for running ones
            executor.shutdown(wait=wait)
            
            # If wait=True, this returns after all tasks complete or timeout
            if wait:
                import time
                deadline = time.time() + timeout
                while self._active_executor is not None and time.time() < deadline:
                    time.sleep(0.1)
                
                if self._active_executor is not None:
                    log.error("GTM: Parallel operation shutdown timeout after {}s".format(timeout))
                    return False
                else:
                    log.info("GTM: Parallel operations shutdown successfully")
                    return True
            else:
                with self._executor_lock:
                    self._active_executor = None
                log.info("GTM: Parallel operations shutdown initiated (not waiting)")
                return True
                
        except Exception as e:
            log.error("GTM: Error during parallel operation shutdown: {}".format(str(e)))
            with self._executor_lock:
                self._active_executor = None
            return False
    
    def is_operation_active(self):
        """Check if parallel operations are currently running.
        
        Returns:
            bool: True if operations are active, False otherwise
            
        Use this before starting new configuration changes to avoid conflicts.
        """
        with self._executor_lock:
            return self._active_executor is not None
    
    # ========================================================================
    # CLEANUP METHODS
    # ========================================================================
    
    def cleanup_infrastructure_from_bigip(self, expected_members):
        """Clean up VSs and servers by comparing BIG-IP state with expected config.
        
        Single-pass cleanup that removes orphaned virtual servers and servers
        with no remaining virtual servers.
        
        Args:
            expected_members (dict): Expected pool members {pool_name: set(member_refs)}
            
        Note:
            This is called during initial sync/restart to clean up resources
            that are no longer in the configuration.
        """
        try:
            all_expected_members = set()
            all_expected_servers = set()
            for pool_name, member_set in expected_members.items():
                all_expected_members.update(member_set)
                for member_ref in member_set:
                    server_name = member_ref.split(':')[0]
                    all_expected_servers.add(server_name)

            log.debug("GTM: Expected members after create: {}".format(all_expected_members))
            log.debug("GTM: Expected servers after create: {}".format(all_expected_servers))

            for server_name in all_expected_servers:
                try:
                    if not self._gtm.servers.server.exists(name=server_name):
                        continue

                    server = self._gtm.servers.server.load(name=server_name)

                    vs_list = list(server.virtual_servers_s.get_collection())

                    remaining_vs = []
                    for vs in vs_list:
                        member_ref = "{}:{}".format(server_name, vs.name)
                        if member_ref not in all_expected_members:
                            log.info("GTM: Deleting orphaned VS {} from server {} (restart cleanup)".format(
                                vs.name, server_name))
                            vs.delete()
                        else:
                            remaining_vs.append(vs)

                    if len(remaining_vs) == 0:
                        log.info("GTM: Deleting server {} with no VSs (restart cleanup)".format(
                            server_name))
                        server.delete()

                except Exception as e:
                    log.error("GTM: Error processing server {} during restart cleanup: {}".format(
                        server_name, str(e)))
                    raise F5CcclError(msg="Restart cleanup failed for server {}: {}".format(
                        server_name, str(e)))

        except Exception as e:
            log.error("GTM: Error during infrastructure cleanup from BIG-IP: {}".format(str(e)))
            raise F5CcclError(msg="Infrastructure cleanup from BIG-IP failed: {}".format(str(e)))
    
    # ========================================================================
    # PARALLEL EXECUTION METHODS (Performance Optimization)
    # ========================================================================
    
    def create_servers_parallel(self, servers_to_create, datacenter_name, 
                                max_workers=5, monitor='/Common/gateway_icmp'):
        """Create multiple GSLB servers in parallel using ThreadPoolExecutor.
        
        Args:
            servers_to_create (list): List of (server_name, ip_address) tuples
            datacenter_name (str): Datacenter to place servers in
            max_workers (int): Maximum parallel threads (default: 5, max recommended: 10)
            monitor (str): Health monitor path
            
        Returns:
            dict: {server_name: server_object} for successfully created servers
            
        Performance:
            - Sequential: ~1s per server → 100s for 100 servers
            - Parallel (5 workers): ~20s for 100 servers (5x faster)
            
        NOTE: Higher worker counts (>10) can overwhelm BIG-IP causing:
              - 502 Proxy Errors
              - Connection resets
              - Connection pool exhaustion
            
        Thread Safety:
            - Uses thread locks for shared state access
            - Each thread operates on independent BIG-IP resources
            - iControl REST is stateless and handles concurrent requests
            - Timeout enforced per operation (default: 5 minutes)
            
        Cleanup:
            - ThreadPoolExecutor ensures all threads complete or timeout
            - Failed operations logged but don't block other operations
            - Executor properly shutdown on completion or exception
        """
        if not servers_to_create:
            return {}
        
        created_servers = {}
        errors = []
        lock = threading.Lock()  # Protect shared state
        
        def _create_server_task(server_name, ip_address):
            """Task for parallel execution."""
            try:
                server = self.create_gslb_server(
                    server_name=server_name,
                    datacenter_name=datacenter_name,
                    addresses=[ip_address],
                    product='bigip',
                    virtual_server_discovery='disabled',
                    monitor=monitor)
                return (server_name, server, None)
            except Exception as e:
                log.error("GTM: [PARALLEL] Error creating server {}: {}".format(
                    server_name, str(e)))
                return (server_name, None, str(e))
        
        log.info("GTM: [PARALLEL] Creating {} servers with {} workers (timeout: {}s per operation)".format(
            len(servers_to_create), max_workers, self._operation_timeout))
        
        start_time = time.time()
        executor = None
        
        try:
            executor = ThreadPoolExecutor(max_workers=max_workers)
            with self._executor_lock:
                self._active_executor = executor  # Track for cleanup
            
            # Submit all tasks
            future_to_server = {
                executor.submit(_create_server_task, srv_name, ip_addr): srv_name
                for srv_name, ip_addr in servers_to_create
            }
            
            # Collect results as they complete with timeout
            for future in as_completed(future_to_server, timeout=self._operation_timeout):
                try:
                    server_name, server_obj, error = future.result(timeout=30)  # Individual task timeout
                    with lock:  # Thread-safe shared state access
                        if error:
                            errors.append((server_name, error))
                        elif server_obj:
                            created_servers[server_name] = server_obj
                except TimeoutError:
                    server_name = future_to_server.get(future, 'unknown')
                    log.error("GTM: [PARALLEL] Timeout creating server {}".format(server_name))
                    with lock:
                        errors.append((server_name, "Operation timeout"))
                except Exception as e:
                    server_name = future_to_server.get(future, 'unknown')
                    log.error("GTM: [PARALLEL] Unexpected error for server {}: {}".format(
                        server_name, str(e)))
                    with lock:
                        errors.append((server_name, str(e)))
        
        except TimeoutError:
            log.error("GTM: [PARALLEL] Overall timeout waiting for server creation")
            with lock:
                errors.append(("OVERALL", "Timeout waiting for all servers"))
        except Exception as e:
            log.error("GTM: [PARALLEL] Critical error in parallel server creation: {}".format(str(e)))
            raise
        finally:
            # CRITICAL: Ensure executor is always shutdown
            if executor:
                executor.shutdown(wait=True)  # Wait for running tasks
                with self._executor_lock:
                    self._active_executor = None
                log.debug("GTM: [PARALLEL] Executor shutdown complete for server creation")
        
        elapsed = time.time() - start_time
        
        log.info("GTM: [PARALLEL] Server creation complete: {} succeeded, {} failed in {:.1f}s".format(
            len(created_servers), len(errors), elapsed))
        
        if errors:
            log.warning("GTM: [PARALLEL] Server creation errors: {}".format(
                [(name, err) for name, err in errors[:5]]))
        
        return created_servers
    
    def create_virtual_servers_parallel(self, vs_tasks, max_workers=8):
        """Create multiple virtual servers in parallel.
        
        Args:
            vs_tasks (list): List of tuples:
                             (server_name, server_obj, vs_name, destination)
            max_workers (int): Maximum parallel threads (default: 8, max recommended: 12)
            
        Returns:
            dict: {vs_name: vs_object} for successfully created VSs
            
        Performance:
            - Sequential: ~0.5s per VS → 500s for 1000 VSs
            - Parallel (8 workers): ~63s for 1000 VSs (8x faster)
            
        NOTE: Higher worker counts (>12) can overwhelm BIG-IP causing:
              - 502 Proxy Errors  
              - Connection aborted / Remote disconnected
              - urllib3 connection pool exhaustion (default: 10 connections)
              
        Thread Safety:
            - Uses thread locks for shared state access
            - Each thread operates on pre-loaded server objects
            - Timeout enforced per operation
            
        Cleanup:
            - Executor properly shutdown on completion or exception
            - Failed operations logged but don't block other operations
            - All threads guaranteed to complete or timeout
        """
        if not vs_tasks:
            return {}
        
        created_vs = {}
        errors = []
        lock = threading.Lock()  # Protect shared state
        
        def _create_vs_task(server_name, server_obj, vs_name, destination):
            """Task for parallel VS creation with retry logic."""
            for attempt in range(self._max_retries):
                try:
                    vs = self.create_virtual_server_on_gslb_server(
                        server_name=server_name,
                        vs_name=vs_name,
                        destination=destination,
                        enabled=True,
                        server_obj=server_obj)
                    return (vs_name, vs, None)
                except Exception as e:
                    error_str = str(e)
                    is_transient = (
                        '502' in error_str or 
                        'Proxy Error' in error_str or
                        'Connection aborted' in error_str or
                        'Remote end closed' in error_str or
                        'RemoteDisconnected' in error_str
                    )
                    
                    if is_transient and attempt < self._max_retries - 1:
                        # Exponential backoff: 2^attempt * backoff_base seconds
                        delay = (self._retry_backoff_base ** attempt)
                        log.warning("GTM: Transient error creating VS {}, retry {}/{} after {:.1f}s: {}".format(
                            vs_name, attempt + 1, self._max_retries, delay, error_str[:100]))
                        time.sleep(delay)
                        continue
                    else:
                        # Non-transient or final retry failed
                        log.error("GTM: Error creating VS {} (attempt {}/{}): {}".format(
                            vs_name, attempt + 1, self._max_retries, error_str[:200]))
                        return (vs_name, None, str(e))
            
            return (vs_name, None, "Max retries exceeded")
        
        log.info("GTM:  Creating {} virtual servers with {} workers (timeout: {}s per operation)".format(
            len(vs_tasks), max_workers, self._operation_timeout))
        
        start_time = time.time()
        executor = None
        
        try:
            executor = ThreadPoolExecutor(max_workers=max_workers)
            with self._executor_lock:
                self._active_executor = executor  # Track for cleanup
            
            # Submit tasks with rate limiting to avoid overwhelming BIG-IP
            future_to_vs = {}
            for i, (srv_name, srv_obj, vs_name, dest) in enumerate(vs_tasks):
                future = executor.submit(_create_vs_task, srv_name, srv_obj, vs_name, dest)
                future_to_vs[future] = vs_name
                
                # Rate limit: small delay between submissions (default 20ms)
                if i > 0 and i % max_workers == 0:  # Every batch of max_workers
                    time.sleep(self._task_submission_delay)
            
            log.info("GTM: All {} VS creation tasks submitted, waiting for completion...".format(len(vs_tasks)))
            
            # Collect results as they complete with timeout
            completed = 0
            successes = 0
            start_time_collection = time.time()
            for future in as_completed(future_to_vs, timeout=self._operation_timeout):
                try:
                    vs_name, vs_obj, error = future.result(timeout=30)  # Individual task timeout
                    completed += 1
                    
                    if error is None and vs_obj:
                        successes += 1
                    
                    # Progress logging every 5% or 100 VSs (whichever is smaller for more frequent updates)
                    progress_interval = min(100, max(10, len(vs_tasks) // 20))  # At least every 5%
                    if completed % progress_interval == 0 or completed == len(vs_tasks):
                        elapsed = time.time() - start_time_collection
                        rate = completed / elapsed if elapsed > 0 else 0
                        log.info("GTM: VS Progress: {}/{} completed ({:.1f}%, {:.1f}/sec, {} succeeded, {} failed)".format(
                            completed, len(vs_tasks), 100.0 * completed / len(vs_tasks), rate, successes, len(errors)))
                    
                    with lock:  # Thread-safe shared state access
                        if error:
                            errors.append((vs_name, error))
                        elif vs_obj:
                            created_vs[vs_name] = vs_obj
                except TimeoutError:
                    vs_name = future_to_vs.get(future, 'unknown')
                    log.error("GTM: Timeout creating VS {}".format(vs_name))
                    with lock:
                        errors.append((vs_name, "Operation timeout"))
                except Exception as e:
                    vs_name = future_to_vs.get(future, 'unknown')
                    log.error("GTM: Unexpected error for VS {}: {}".format(
                        vs_name, str(e)))
                    with lock:
                        errors.append((vs_name, str(e)))
        
        except TimeoutError:
            log.error("GTM: Overall timeout waiting for VS creation")
            with lock:
                errors.append(("OVERALL", "Timeout waiting for all VSs"))
        except Exception as e:
            log.error("GTM:Critical error in parallel VS creation: {}".format(str(e)))
            raise
        finally:
            # CRITICAL: Ensure executor is always shutdown
            if executor:
                executor.shutdown(wait=True)  # Wait for running tasks
                with self._executor_lock:
                    self._active_executor = None
                log.debug("GTM: Executor shutdown complete for VS creation")
        
        elapsed = time.time() - start_time
        
        # CRITICAL: Raise on significant failures instead of silently returning partial results
        failure_rate = len(errors) / len(vs_tasks) if vs_tasks else 0
        if failure_rate > 0.1:  # More than 10% failures
            log.error("GTM: VS creation failed: {} succeeded, {} failed ({:.1%} failure rate) in {:.1f}s".format(
                len(created_vs), len(errors), failure_rate, elapsed))
            log.error("GTM: First 10 failures: {}".format(errors[:10]))
            raise F5CcclError(msg="VS creation failure rate too high: {}/{} failed ({:.1%})".format(
                len(errors), len(vs_tasks), failure_rate))
        
        log.info("GTM: VS creation complete: {} succeeded, {} failed in {:.1f}s".format(
            len(created_vs), len(errors), elapsed))
        
        if errors and len(errors) <= 10:
            log.warning("GTM: VS creation errors: {}".format(errors))
        elif errors:
            log.warning("GTM: VS creation: {} errors (showing first 10): {}".format(
                len(errors), errors[:10]))
        
        return created_vs
    
    def orchestrate_with_snapshot_parallel(self, gtmConfig, parsed, snapshot, 
                                          server_workers=5, vs_workers=8):
        """Parallel-optimized orchestration using ThreadPoolExecutor.
        
        This method provides significant performance improvements over sequential
        orchestration, especially for large deployments (100+ servers, 1000+ VSs).
        
        Args:
            gtmConfig (dict): Full GTM configuration
            parsed (dict): Pre-parsed config data
            snapshot (dict): BIG-IP state snapshot
            server_workers (int): Parallel threads for servers (default: 5, max: 10)
            vs_workers (int): Parallel threads for VSs (default: 8, max: 12)
            
        Returns:
            dict: Summary with keys:
                  - datacenter: Datacenter name
                  - servers: Number of servers managed
                  - virtual_servers: Number of VSs created/managed
                  
        Performance Comparison (example: 100 servers, 1000 VSs):
            - Sequential: ~600s total (10 min)
            - Parallel (5/8 workers): ~90s total (1.5 min) → 6-7x faster
            
        Note:
            Uses snapshot to skip existing resources, then creates missing
            resources in parallel batches.
        """
        try:
            datacenter_name = gtmConfig[self._partition].get("dataCenter", None)
            if not datacenter_name:
                raise F5CcclError(msg="GTM: dataCenter not specified for partition {}".format(self._partition))
            if "/" in datacenter_name:
                datacenter_name = datacenter_name.split("/")[-1]

            self.ensure_datacenter_exists(datacenter_name)

            dataservers = parsed['dataservers']
            vs_inventory = parsed['vs_inventory']

            if not dataservers:
                return {"datacenter": datacenter_name, "servers": 0, "virtual_servers": 0}

            log.info("GTM: [ORCHESTRATE] Processing {} dataservers".format(
                len(dataservers)))

            # ---------------------------------------------------------------
            # PHASE 1: Server creation (parallel for missing servers)
            # ---------------------------------------------------------------
            servers_to_create = []
            server_objects = {}
            servers_skipped = 0

            for dataserver_ip in sorted(dataservers):
                server_name = GTMUtils.format_server_name(dataserver_ip)
                
                if server_name in snapshot['servers']:
                    # Load existing server for VS creation
                    server_objects[server_name] = self._gtm.servers.server.load(name=server_name)
                    servers_skipped += 1
                else:
                    # Queue for parallel creation
                    servers_to_create.append((server_name, dataserver_ip))

            # Create missing servers in parallel
            if servers_to_create:
                created = self.create_servers_parallel(
                    servers_to_create, 
                    datacenter_name, 
                    max_workers=server_workers)
                server_objects.update(created)
                log.info("GTM: [ORCHESTRATE] Servers: {} created parallel, {} skipped".format(
                    len(created), servers_skipped))
            else:
                log.info("GTM: [ORCHESTRATE] Servers: 0 created, {} skipped (all exist)".format(
                    servers_skipped))

            # ---------------------------------------------------------------
            # PHASE 2: VS creation (parallel for missing VSs)
            # ---------------------------------------------------------------
            vs_tasks = []
            total_vs_skipped = 0

            for server_name, vs_set in vs_inventory.items():
                if server_name not in server_objects:
                    continue

                server_obj = server_objects[server_name]

                # Lazy load existing VSs for this server
                existing_vs = snapshot['server_vs'].get(server_name, set())
                if not existing_vs:
                    try:
                        existing_vs = {vs.name for vs in server_obj.virtual_servers_s.get_collection()}
                        snapshot['server_vs'][server_name] = existing_vs
                        log.debug("GTM: Lazy-loaded {} VSs for server {}".format(
                            len(existing_vs), server_name))
                    except Exception as e:
                        if GTMUtils.is_transient_error(e):
                            log.warning("GTM: Transient error fetching VSs for {}: {}".format(
                                server_name, str(e)))
                            raise F5CcclError(msg="VS fetch failed: {}".format(str(e)))
                        else:
                            existing_vs = set()

                # Queue missing VSs for parallel creation
                for member_ip, vs_name, destination in vs_set:
                    if vs_name in existing_vs:
                        total_vs_skipped += 1
                    else:
                        vs_tasks.append((server_name, server_obj, vs_name, destination))

            # Create missing VSs in parallel
            total_vs_created = 0
            if vs_tasks:
                created_vs = self.create_virtual_servers_parallel(
                    vs_tasks, 
                    max_workers=vs_workers)
                total_vs_created = len(created_vs)
                log.info("GTM: [ORCHESTRATE] VSs: {} created parallel, {} skipped".format(
                    total_vs_created, total_vs_skipped))
            else:
                log.info("GTM: [ORCHESTRATE] VSs: 0 created, {} skipped (all exist)".format(
                    total_vs_skipped))

            return {
                "datacenter": datacenter_name,
                "servers": len(server_objects),
                "virtual_servers": total_vs_created + total_vs_skipped
            }

        except Exception as e:
            log.error("GTM: Critical error during parallel orchestration: {}".format(str(e)))
            raise F5CcclError(msg="Parallel orchestration failed: {}".format(str(e)))
