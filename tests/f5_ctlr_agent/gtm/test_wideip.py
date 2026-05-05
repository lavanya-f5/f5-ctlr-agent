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

"""Unit tests for GTMWideIP - WideIP operations."""

import pytest
from unittest.mock import Mock, MagicMock
from f5_cccl.exceptions import F5CcclError
from f5_ctlr_agent.gtm.wideip import GTMWideIP


@pytest.fixture
def mock_gtm():
    """Create a mock GTM object."""
    gtm = Mock()
    gtm.wideips.a_s.a.exists = Mock(return_value=False)
    gtm.wideips.a_s.a.create = Mock()
    gtm.wideips.a_s.a.load = Mock()
    return gtm


@pytest.fixture
def wideip_manager(mock_gtm):
    """Create GTMWideIP instance."""
    return GTMWideIP(mock_gtm, "Common")


class TestCreateWideip:
    """Tests for GTMWideIP.create_wideip()."""
    
    def test_create_new_wideip(self, wideip_manager, mock_gtm):
        """Test creating a new wideIP."""
        config = {
            "name": "example.com",
            "LoadBalancingMode": "round-robin"
        }
        pools = [{"name": "pool1", "partition": "Common", "ratio": 1, "order": 0}]
        
        wideip_manager.attach_pool_to_wideip = Mock()
        
        wideip_manager.create_wideip(config, pools)
        
        mock_gtm.wideips.a_s.a.create.assert_called_once_with(
            name="example.com",
            partition="Common",
            lastResortPool="none",
            poolLbMode="round-robin"
        )
        wideip_manager.attach_pool_to_wideip.assert_called_once()
    
    def test_update_existing_wideip(self, wideip_manager, mock_gtm):
        """Test updating existing wideIP."""
        mock_gtm.wideips.a_s.a.exists.return_value = True
        mock_wideip = Mock()
        mock_wideip.poolLbMode = "ratio"
        mock_wideip.raw = {'pools': {}}
        mock_gtm.wideips.a_s.a.load.return_value = mock_wideip
        
        config = {
            "name": "example.com",
            "LoadBalancingMode": "round-robin"
        }
        pools = [{"name": "pool1"}]
        
        wideip_manager.attach_pool_to_wideip = Mock()
        
        wideip_manager.create_wideip(config, pools)
        
        # Should update load balancing mode
        assert mock_wideip.poolLbMode == "round-robin"
        mock_wideip.update.assert_called_once()
    
    def test_avoid_duplicate_pools(self, wideip_manager, mock_gtm):
        """Test that duplicate pools are not added."""
        mock_gtm.wideips.a_s.a.exists.return_value = True
        mock_wideip = Mock()
        mock_wideip.poolLbMode = "round-robin"
        mock_wideip.raw = {'pools': {'pool1': {}}}
        mock_wideip.pools = [{"name": "pool1"}]
        mock_gtm.wideips.a_s.a.load.return_value = mock_wideip
        
        config = {
            "name": "example.com",
            "LoadBalancingMode": "round-robin"
        }
        pools = [{"name": "pool1"}, {"name": "pool2"}]
        
        wideip_manager.attach_pool_to_wideip = Mock()
        
        wideip_manager.create_wideip(config, pools)
        
        # Should only attach pool2 (pool1 already exists)
        call_args = wideip_manager.attach_pool_to_wideip.call_args
        attached_pools = call_args[0][1]
        assert len(attached_pools) == 1
        assert attached_pools[0]["name"] == "pool2"


class TestAttachPoolToWideip:
    """Tests for GTMWideIP.attach_pool_to_wideip()."""
    
    def test_attach_pool_with_pools_attribute(self, wideip_manager, mock_gtm):
        """Test attaching pool when wideIP has pools attribute."""
        mock_wideip = Mock()
        mock_wideip.lastResortPool = ""
        mock_wideip.pools = []
        mock_gtm.wideips.a_s.a.load.return_value = mock_wideip
        
        pools = [{"name": "pool1", "partition": "Common"}]
        
        wideip_manager.attach_pool_to_wideip("example.com", pools)
        
        assert mock_wideip.lastResortPool == "none"
        assert len(mock_wideip.pools) == 1
        mock_wideip.update.assert_called_once()
    
    def test_attach_pool_without_pools_attribute(self, wideip_manager, mock_gtm):
        """Test attaching pool when wideIP has no pools attribute."""
        mock_wideip = Mock()
        mock_wideip.lastResortPool = ""
        mock_wideip.raw = {}
        delattr(mock_wideip, 'pools')  # Remove pools attribute
        mock_gtm.wideips.a_s.a.load.return_value = mock_wideip
        
        pools = [{"name": "pool1"}]
        
        wideip_manager.attach_pool_to_wideip("example.com", pools)
        
        assert mock_wideip.raw['pools'] == pools
        mock_wideip.update.assert_called_once()


class TestRemovePoolFromWideip:
    """Tests for GTMWideIP.remove_pool_from_wideip()."""
    
    def test_remove_pool_successfully(self, wideip_manager, mock_gtm):
        """Test removing a pool from wideIP."""
        mock_wideip = Mock()
        mock_wideip.lastResortPool = ""
        mock_wideip.pools = [{"name": "pool1"}, {"name": "pool2"}]
        mock_gtm.wideips.a_s.a.exists.return_value = True
        mock_gtm.wideips.a_s.a.load.return_value = mock_wideip
        
        wideip_manager.remove_pool_from_wideip("example.com", "pool1")
        
        assert len(mock_wideip.pools) == 1
        assert mock_wideip.pools[0]["name"] == "pool2"
        mock_wideip.update.assert_called_once()
    
    def test_remove_pool_wideip_not_found(self, wideip_manager, mock_gtm):
        """Test removing pool when wideIP doesn't exist (success)."""
        mock_gtm.wideips.a_s.a.exists.return_value = False
        
        # Should not raise - wideIP not existing means pool already removed
        wideip_manager.remove_pool_from_wideip("example.com", "pool1")
    
    def test_remove_pool_already_removed(self, wideip_manager, mock_gtm):
        """Test removing pool that's not in wideIP."""
        mock_wideip = Mock()
        mock_wideip.lastResortPool = "none"
        mock_wideip.pools = [{"name": "pool2"}]
        mock_gtm.wideips.a_s.a.exists.return_value = True
        mock_gtm.wideips.a_s.a.load.return_value = mock_wideip
        
        wideip_manager.remove_pool_from_wideip("example.com", "pool1")
        
        # Should not call update if pool not found
        mock_wideip.update.assert_not_called()


class TestDeleteWideip:
    """Tests for GTMWideIP.delete_wideip()."""
    
    def test_delete_wideip_no_pools(self, wideip_manager, mock_gtm):
        """Test deleting wideIP with no pools attached."""
        mock_wideip = Mock()
        mock_wideip.lastResortPool = ""
        mock_wideip.pools = []
        mock_gtm.wideips.a_s.a.exists.return_value = True
        mock_gtm.wideips.a_s.a.load.return_value = mock_wideip
        
        wideip_manager.delete_wideip("example.com")
        
        mock_wideip.delete.assert_called_once()
    
    def test_delete_wideip_with_pools_attached(self, wideip_manager, mock_gtm):
        """Test deleting wideIP that still has pools (should not delete)."""
        mock_wideip = Mock()
        mock_wideip.lastResortPool = "none"
        mock_wideip.pools = [{"name": "pool1"}]
        mock_gtm.wideips.a_s.a.exists.return_value = True
        mock_gtm.wideips.a_s.a.load.return_value = mock_wideip
        
        wideip_manager.delete_wideip("example.com")
        
        # Should not delete if pools still attached
        mock_wideip.delete.assert_not_called()
    
    def test_delete_wideip_already_deleted(self, wideip_manager, mock_gtm):
        """Test deleting wideIP that doesn't exist (404)."""
        mock_gtm.wideips.a_s.a.exists.return_value = False
        
        # Should not raise - already deleted is success
        wideip_manager.delete_wideip("example.com")


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
