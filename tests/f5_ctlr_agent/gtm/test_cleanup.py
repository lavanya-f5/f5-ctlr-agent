#!/usr/bin/env python

# Copyright (c) 2018-2021 F5 Networks, Inc.
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for GTMCleanup - Resource cleanup operations."""

import pytest
from unittest.mock import Mock, MagicMock
from f5_cccl.exceptions import F5CcclError
from f5_ctlr_agent.gtm.cleanup import GTMCleanup


@pytest.fixture
def mock_gtm():
    """Create a mock GTM object."""
    gtm = Mock()
    gtm.servers.server.exists = Mock(return_value=True)
    gtm.servers.server.load = Mock()
    gtm.servers.get_collection = Mock(return_value=[])
    return gtm


@pytest.fixture
def mock_pool_manager():
    """Create a mock GTMPool instance."""
    return Mock()


@pytest.fixture
def cleanup_manager(mock_gtm, mock_pool_manager):
    """Create GTMCleanup instance."""
    return GTMCleanup(mock_gtm, "Common", pool_manager=mock_pool_manager)


class TestCleanupOrphanedMembers:
    """Tests for GTMCleanup.cleanup_orphaned_members_with_snapshot()."""
    
    def test_cleanup_orphaned_members(self, cleanup_manager, mock_gtm, mock_pool_manager):
        """Test cleaning up orphaned members."""
        expected_members = {
            "pool1": {"server-10-0-0-1:vs-192-168-1-1-80"}
        }
        snapshot = {
            "pool_members": {
                "pool1": {
                    "server-10-0-0-1:vs-192-168-1-1-80",
                    "server-10-0-0-1:vs-192-168-1-2-80"  # Orphan
                }
            }
        }
        
        mock_pool = Mock()
        mock_gtm.pools.a_s.a.load.return_value = mock_pool
        
        cleanup_manager.cleanup_orphaned_members_with_snapshot(expected_members, snapshot)
        
        # Should remove the orphaned member
        mock_pool_manager.remove_member.assert_called_once()
    
    def test_cleanup_no_orphans(self, cleanup_manager, mock_gtm, mock_pool_manager):
        """Test cleanup when there are no orphans."""
        expected_members = {
            "pool1": {"server-10-0-0-1:vs-192-168-1-1-80"}
        }
        snapshot = {
            "pool_members": {
                "pool1": {"server-10-0-0-1:vs-192-168-1-1-80"}
            }
        }
        
        cleanup_manager.cleanup_orphaned_members_with_snapshot(expected_members, snapshot)
        
        # Should not remove anything
        mock_pool_manager.remove_member.assert_not_called()


class TestCleanupUnusedVirtualServers:
    """Tests for GTMCleanup.cleanup_unused_virtual_servers()."""
    
    def test_cleanup_unused_vs(self, cleanup_manager, mock_gtm):
        """Test cleaning up unused virtual servers."""
        old_config = {
            "Common": {
                "wideIPs": [{
                    "name": "example.com",
                    "pools": [{
                        "name": "pool1",
                        "members": ["10.0.0.1|192.168.1.1|80", "10.0.0.1|192.168.1.2|80"]
                    }]
                }]
            }
        }
        new_config = {
            "Common": {
                "wideIPs": [{
                    "name": "example.com",
                    "pools": [{
                        "name": "pool1",
                        "members": ["10.0.0.1|192.168.1.1|80"]  # One member removed
                    }]
                }]
            }
        }
        
        mock_server = Mock()
        mock_vs1 = Mock()
        mock_vs1.name = "vs-192-168-1-1-80"
        mock_vs2 = Mock()
        mock_vs2.name = "vs-192-168-1-2-80"
        mock_server.virtual_servers_s.get_collection.return_value = [mock_vs1, mock_vs2]
        
        mock_gtm.servers.server.load.return_value = mock_server
        
        cleanup_manager.cleanup_unused_virtual_servers(old_config, new_config)
        
        # Should delete the unused VS
        mock_vs2.delete.assert_called_once()
    
    def test_cleanup_vs_no_changes(self, cleanup_manager, mock_gtm):
        """Test cleanup when no VSs need to be removed."""
        config = {
            "Common": {
                "wideIPs": [{
                    "name": "example.com",
                    "pools": [{
                        "name": "pool1",
                        "members": ["10.0.0.1|192.168.1.1|80"]
                    }]
                }]
            }
        }
        
        cleanup_manager.cleanup_unused_virtual_servers(config, config)
        
        # Should not delete anything
        mock_gtm.servers.server.load.assert_not_called()


class TestCleanup UnusedGslbServers:
    """Tests for GTMCleanup.cleanup_unused_gslb_servers()."""
    
    def test_cleanup_server_with_no_vs(self, cleanup_manager, mock_gtm):
        """Test cleaning up server with no virtual servers."""
        old_config = {
            "Common": {
                "wideIPs": [{
                    "name": "example.com",
                    "pools": [{
                        "name": "pool1",
                        "members": ["10.0.0.1|192.168.1.1|80"]
                    }]
                }]
            }
        }
        new_config = {
            "Common": {
                "wideIPs": []
            }
        }
        
        mock_server = Mock()
        mock_server.name = "server-10-0-0-1"
        mock_server.virtual_servers_s.get_collection.return_value = []  # No VSs
        mock_gtm.servers.get_collection.return_value = [mock_server]
        
        cleanup_manager.cleanup_unused_gslb_servers(None, old_config, new_config)
        
        # Should delete server with no VSs
        mock_server.delete.assert_called_once()
    
    def test_cleanup_server_with_vs_remaining(self, cleanup_manager, mock_gtm):
        """Test not deleting server that still has VSs."""
        old_config = {
            "Common": {
                "wideIPs": [{
                    "name": "example.com",
                    "pools": [{
                        "name": "pool1",
                        "members": ["10.0.0.1|192.168.1.1|80", "10.0.0.1|192.168.1.2|80"]
                    }]
                }]
            }
        }
        new_config = {
            "Common": {
                "wideIPs": [{
                    "name": "example.com",
                    "pools": [{
                        "name": "pool1",
                        "members": ["10.0.0.1|192.168.1.1|80"]  # One member still exists
                    }]
                }]
            }
        }
        
        mock_server = Mock()
        mock_server.name = "server-10-0-0-1"
        mock_vs = Mock()
        mock_server.virtual_servers_s.get_collection.return_value = [mock_vs]  # Has VS
        mock_gtm.servers.get_collection.return_value = [mock_server]
        
        cleanup_manager.cleanup_unused_gslb_servers(None, old_config, new_config)
        
        # Should NOT delete server that still has VSs
        mock_server.delete.assert_not_called()


class TestRetryPendingCleanup:
    """Tests for GTMCleanup.retry_pending_cleanup()."""
    
    def test_retry_successful(self, cleanup_manager, mock_gtm):
        """Test successful retry of pending cleanup."""
        pending_cleanup_state = {
            'partition': 'Common',
            'oldConfig': {},
            'target_config': {},
            'old_parsed': None,
            'new_parsed': None,
            'datacenter_name': 'dc1'
        }
        
        cleanup_manager.cleanup_unused_virtual_servers = Mock()
        cleanup_manager.cleanup_unused_gslb_servers = Mock()
        
        result = cleanup_manager.retry_pending_cleanup(pending_cleanup_state)
        
        assert result is True
        cleanup_manager.cleanup_unused_virtual_servers.assert_called_once()
        cleanup_manager.cleanup_unused_gslb_servers.assert_called_once()
    
    def test_retry_no_pending_cleanup(self, cleanup_manager, mock_gtm):
        """Test retry when there's no pending cleanup."""
        result = cleanup_manager.retry_pending_cleanup(None)
        
        assert result is True
    
    def test_retry_fails(self, cleanup_manager, mock_gtm):
        """Test retry that fails."""
        pending_cleanup_state = {
            'partition': 'Common',
            'oldConfig': {},
            'target_config': {},
            'old_parsed': None,
            'new_parsed': None,
            'datacenter_name': 'dc1'
        }
        
        cleanup_manager.cleanup_unused_virtual_servers = Mock(side_effect=Exception("VS cleanup failed"))
        cleanup_manager.cleanup_unused_gslb_servers = Mock()
        
        with pytest.raises(F5CcclError):
            cleanup_manager.retry_pending_cleanup(pending_cleanup_state)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
