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

"""Unit tests for GTMPool - Pool and member management."""

import pytest
from unittest.mock import Mock, MagicMock, call
from f5_cccl.exceptions import F5CcclError
from f5_ctlr_agent.gtm.pool import GTMPool


@pytest.fixture
def mock_gtm():
    """Create a mock GTM object."""
    gtm = Mock()
    gtm.pools.a_s.a.exists = Mock(return_value=False)
    gtm.pools.a_s.a.create = Mock()
    gtm.pools.a_s.a.load = Mock()
    return gtm


@pytest.fixture
def pool_manager(mock_gtm):
    """Create GTMPool instance."""
    return GTMPool(mock_gtm, "Common", active_tenants=["tenant1"], deleted_tenants=[])


class TestCreatePool:
    """Tests for GTMPool.create_pool()."""
    
    def test_create_new_pool(self, pool_manager, mock_gtm):
        """Test creating a new pool."""
        mock_pool = Mock()
        mock_pool.fallbackMode = "return-to-dns"
        mock_pool.loadBalancingMode = "round-robin"
        mock_gtm.pools.a_s.a.create.return_value = mock_pool
        
        config = {
            "pools": [
                {
                    "name": "pool1",
                    "fallbackMode": "return-to-dns",
                    "LoadBalancingMode": "round-robin",
                    "members": []
                }
            ]
        }
        
        pool_manager.create_pool(config, "")
        
        mock_gtm.pools.a_s.a.create.assert_called_once_with(
            name="pool1",
            partition="Common",
            fallbackMode="return-to-dns",
            loadBalancingMode="round-robin"
        )
    
    def test_update_existing_pool(self, pool_manager, mock_gtm):
        """Test updating existing pool with new attributes."""
        mock_gtm.pools.a_s.a.exists.return_value = True
        mock_pool = Mock()
        mock_pool.fallbackMode = "none"
        mock_pool.loadBalancingMode = "ratio"
        mock_gtm.pools.a_s.a.load.return_value = mock_pool
        
        config = {
            "pools": [
                {
                    "name": "pool1",
                    "fallbackMode": "return-to-dns",
                    "LoadBalancingMode": "round-robin",
                    "members": []
                }
            ]
        }
        
        pool_manager.create_pool(config, "/Common/http")
        
        # Should update attributes and call update()
        assert mock_pool.fallbackMode == "return-to-dns"
        assert mock_pool.loadBalancingMode == "round-robin"
        assert mock_pool.monitor == "/Common/http"
        mock_pool.update.assert_called_once()
    
    def test_create_pool_with_members(self, pool_manager, mock_gtm):
        """Test creating pool with members."""
        mock_pool = Mock()
        mock_pool.fallbackMode = "return-to-dns"
        mock_pool.loadBalancingMode = "round-robin"
        mock_gtm.pools.a_s.a.create.return_value = mock_pool
        
        # Mock add_member to avoid complex member logic
        pool_manager.add_member = Mock()
        
        config = {
            "pools": [
                {
                    "name": "pool1",
                    "fallbackMode": "return-to-dns",
                    "LoadBalancingMode": "round-robin",
                    "members": ["10.0.0.1|192.168.1.1|80"],
                    "DataServer": None
                }
            ]
        }
        
        pool_manager.create_pool(config, "", skip_member_validation=True)
        
        # Should call add_member for each member
        assert pool_manager.add_member.called


class TestAddMember:
    """Tests for GTMPool.add_member()."""
    
    def test_add_member_with_validation_skip(self, pool_manager, mock_gtm):
        """Test adding member with validation skipped."""
        mock_pool = Mock()
        mock_pool.members_s.member.create = Mock()
        mock_pool.members_s.member.exists = Mock(return_value=False)
        
        pool_manager.add_member(mock_pool, "pool1", "server-10-0-0-1:vs-192-168-1-1-80", 
                               skip_validation=True)
        
        mock_pool.members_s.member.create.assert_called_once_with(
            name="server-10-0-0-1:vs-192-168-1-1-80",
            partition="Common"
        )
    
    def test_add_member_already_exists_skip_validation(self, pool_manager, mock_gtm):
        """Test adding member that already exists (fast path)."""
        mock_pool = Mock()
        mock_pool.members_s.member.create.side_effect = Exception("already exists in partition Common")
        
        # Should not raise - already exists is success
        pool_manager.add_member(mock_pool, "pool1", "server-10-0-0-1:vs-192-168-1-1-80",
                               skip_validation=True)
    
    def test_add_member_with_validation(self, pool_manager, mock_gtm):
        """Test adding member with full validation."""
        mock_pool = Mock()
        mock_pool.members_s.member.exists = Mock(return_value=False)
        mock_pool.members_s.member.create = Mock()
        
        mock_server = Mock()
        mock_server.virtual_servers_s.virtual_server.exists = Mock(return_value=True)
        
        mock_gtm.servers.server.exists = Mock(return_value=True)
        mock_gtm.servers.server.load = Mock(return_value=mock_server)
        
        pool_manager.add_member(mock_pool, "pool1", "server-10-0-0-1:vs-192-168-1-1-80",
                               skip_validation=False)
        
        mock_pool.members_s.member.create.assert_called_once()


class TestRemoveMember:
    """Tests for GTMPool.remove_member()."""
    
    def test_remove_existing_member(self, pool_manager, mock_gtm):
        """Test removing a member."""
        mock_pool = Mock()
        mock_member = Mock()
        mock_pool.members_s.member.exists.return_value = True
        mock_pool.members_s.member.load.return_value = mock_member
        
        pool_manager.remove_member("pool1", "server-10-0-0-1:vs-192-168-1-1-80", pool_obj=mock_pool)
        
        mock_member.delete.assert_called_once()
    
    def test_remove_nonexistent_member(self, pool_manager, mock_gtm):
        """Test removing member that doesn't exist."""
        mock_pool = Mock()
        mock_pool.members_s.member.exists.return_value = False
        
        # Should not raise
        pool_manager.remove_member("pool1", "server-10-0-0-1:vs-192-168-1-1-80", pool_obj=mock_pool)
    
    def test_remove_member_tenant_check(self, pool_manager, mock_gtm):
        """Test tenant ownership check during removal."""
        mock_pool = Mock()
        
        # Member from different tenant - should skip
        pool_manager.remove_member("pool1", "server:vs/other_tenant/vs1", pool_obj=mock_pool)
        
        # Should not attempt to delete
        mock_pool.members_s.member.exists.assert_not_called()


class TestDeletePool:
    """Tests for GTMPool.delete_pool()."""
    
    def test_delete_pool_with_members(self, pool_manager, mock_gtm):
        """Test deleting pool removes all members first."""
        mock_pool = Mock()
        mock_pool.members_s.get_collection.return_value = []
        
        mock_gtm.pools.a_s.a.exists.return_value = True
        mock_gtm.pools.a_s.a.load.return_value = mock_pool
        
        pool_manager.remove_member = Mock()
        
        working_config = {
            "Common": {
                "wideIPs": [
                    {
                        "name": "example.com",
                        "pools": [
                            {
                                "name": "pool1",
                                "members": ["10.0.0.1|192.168.1.1|80"],
                                "DataServer": None
                            }
                        ]
                    }
                ]
            }
        }
        
        pool_manager.delete_pool("example.com", "pool1", working_config=working_config)
        
        # Should remove members and delete pool
        assert pool_manager.remove_member.called
        mock_pool.delete.assert_called_once()


class TestRemoveMonitorFromPool:
    """Tests for GTMPool.remove_monitor_from_pool()."""
    
    def test_remove_monitor(self, pool_manager, mock_gtm):
        """Test removing monitor from pool."""
        mock_pool = Mock()
        mock_pool.monitor = "/Common/http and /Common/tcp"
        mock_gtm.pools.a_s.a.load.return_value = mock_pool
        
        pool_manager.remove_monitor_from_pool("pool1", "http")
        
        assert mock_pool.monitor == "/Common/tcp"
        mock_pool.update.assert_called_once()
    
    def test_remove_monitor_not_attached(self, pool_manager, mock_gtm):
        """Test removing monitor that's not attached."""
        mock_pool = Mock()
        mock_pool.monitor = "/Common/tcp"
        mock_gtm.pools.a_s.a.load.return_value = mock_pool
        
        pool_manager.remove_monitor_from_pool("pool1", "http")
        
        # Should not call update if monitor not found
        mock_pool.update.assert_not_called()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
