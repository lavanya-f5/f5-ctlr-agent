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

"""Unit tests for GTMSnapshot - BIG-IP state capture."""

import pytest
from unittest.mock import Mock, MagicMock, patch
from f5_ctlr_agent.gtm.snapshot import GTMSnapshot


@pytest.fixture
def mock_gtm():
    """Create a mock GTM object."""
    gtm = Mock()
    gtm.servers.get_collection = Mock(return_value=[])
    gtm.pools.a_s.a.get_collection = Mock(return_value=[])
    gtm.wideips.a_s.a.get_collection = Mock(return_value=[])
    return gtm


@pytest.fixture
def snapshot_helper(mock_gtm):
    """Create GTMSnapshot instance with mock GTM."""
    return GTMSnapshot(mock_gtm, "Common")


class TestSnapshotBigipState:
    """Tests for GTMSnapshot.snapshot_bigip_state()."""
    
    def test_snapshot_empty_bigip(self, snapshot_helper, mock_gtm):
        """Test snapshotting empty BIG-IP."""
        config = {"Common": {"wideIPs": []}}
        
        result = snapshot_helper.snapshot_bigip_state(config)
        
        assert "all_servers" in result
        assert "all_pools" in result
        assert "all_wideips" in result
        assert "pool_members" in result
        assert len(result["all_servers"]) == 0
        assert len(result["all_pools"]) == 0
    
    def test_snapshot_with_servers(self, snapshot_helper, mock_gtm):
        """Test snapshotting with servers."""
        mock_server1 = Mock()
        mock_server1.name = "server-10-0-0-1"
        mock_server2 = Mock()
        mock_server2.name = "server-10-0-0-2"
        
        mock_gtm.servers.get_collection.return_value = [mock_server1, mock_server2]
        
        config = {"Common": {"wideIPs": []}}
        result = snapshot_helper.snapshot_bigip_state(config)
        
        assert len(result["all_servers"]) == 2
        assert "server-10-0-0-1" in result["all_servers"]
        assert "server-10-0-0-2" in result["all_servers"]
    
    def test_snapshot_with_pools_and_members(self, snapshot_helper, mock_gtm):
        """Test snapshotting pools with members."""
        mock_member1 = Mock()
        mock_member1.name = "server-10-0-0-1:vs-192-168-1-1-80"
        mock_member2 = Mock()
        mock_member2.name = "server-10-0-0-1:vs-192-168-1-2-80"
        
        mock_pool = Mock()
        mock_pool.name = "pool1"
        mock_pool.members_s.get_collection.return_value = [mock_member1, mock_member2]
        
        mock_gtm.pools.a_s.a.get_collection.return_value = [mock_pool]
        
        config = {"Common": {"wideIPs": [{"name": "test.com", "pools": [{"name": "pool1"}]}]}}
        result = snapshot_helper.snapshot_bigip_state(config)
        
        assert "pool1" in result["all_pools"]
        assert "pool1" in result["pool_members"]
        assert len(result["pool_members"]["pool1"]) == 2
        assert "server-10-0-0-1:vs-192-168-1-1-80" in result["pool_members"]["pool1"]
    
    def test_snapshot_with_wideips(self, snapshot_helper, mock_gtm):
        """Test snapshotting with wideIPs."""
        mock_wideip = Mock()
        mock_wideip.name = "example.com"
        
        mock_gtm.wideips.a_s.a.get_collection.return_value = [mock_wideip]
        
        config = {"Common": {"wideIPs": [{"name": "example.com"}]}}
        result = snapshot_helper.snapshot_bigip_state(config)
        
        assert len(result["all_wideips"]) == 1
        assert "example.com" in result["all_wideips"]


class TestWideipFullyExists:
    """Tests for GTMSnapshot.wideip_fully_exists()."""
    
    def test_wideip_not_in_snapshot(self, snapshot_helper):
        """Test wideIP that doesn't exist in snapshot."""
        config = {"name": "missing.com", "pools": []}
        snapshot = {"all_wideips": set(), "pool_members": {}}
        
        result = snapshot_helper.wideip_fully_exists(config, snapshot)
        
        assert result is False
    
    def test_wideip_exists_no_pools(self, snapshot_helper):
        """Test wideIP exists with no pools."""
        config = {"name": "example.com", "pools": []}
        snapshot = {"all_wideips": {"example.com"}, "pool_members": {}}
        
        result = snapshot_helper.wideip_fully_exists(config, snapshot)
        
        assert result is True
    
    def test_wideip_exists_with_matching_members(self, snapshot_helper):
        """Test wideIP with all members present."""
        config = {
            "name": "example.com",
            "pools": [
                {
                    "name": "pool1",
                    "members": ["10.0.0.1|192.168.1.1|80"],
                    "DataServer": None
                }
            ]
        }
        snapshot = {
            "all_wideips": {"example.com"},
            "pool_members": {"pool1": {"server-10-0-0-1:vs-192-168-1-1-80"}}
        }
        
        result = snapshot_helper.wideip_fully_exists(config, snapshot)
        
        assert result is True
    
    def test_wideip_exists_missing_members(self, snapshot_helper):
        """Test wideIP exists but missing some members."""
        config = {
            "name": "example.com",
            "pools": [
                {
                    "name": "pool1",
                    "members": ["10.0.0.1|192.168.1.1|80", "10.0.0.1|192.168.1.2|80"]
                }
            ]
        }
        snapshot = {
            "all_wideips": {"example.com"},
            "pool_members": {"pool1": {"server-10-0-0-1:vs-192-168-1-1-80"}}  # Only 1 of 2
        }
        
        result = snapshot_helper.wideip_fully_exists(config, snapshot)
        
        assert result is False
    
    def test_wideip_pool_not_in_snapshot(self, snapshot_helper):
        """Test wideIP with pool that doesn't exist."""
        config = {
            "name": "example.com",
            "pools": [{"name": "missing_pool", "members": []}]
        }
        snapshot = {
            "all_wideips": {"example.com"},
            "pool_members": {}
        }
        
        result = snapshot_helper.wideip_fully_exists(config, snapshot)
        
        assert result is False
    
    def test_wideip_with_dataserver(self, snapshot_helper):
        """Test wideIP with DataServer specified."""
        config = {
            "name": "example.com",
            "pools": [
                {
                    "name": "pool1",
                    "members": ["192.168.1.1:80"],
                    "DataServer": "10.0.0.1"
                }
            ]
        }
        snapshot = {
            "all_wideips": {"example.com"},
            "pool_members": {"pool1": {"server-10-0-0-1:vs-192-168-1-1-80"}}
        }
        
        result = snapshot_helper.wideip_fully_exists(config, snapshot)
        
        assert result is True


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
