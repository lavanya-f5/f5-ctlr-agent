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

"""Unit tests for GTMInfrastructure - Server and VS lifecycle."""

import pytest
from unittest.mock import Mock, MagicMock, call
from f5_cccl.exceptions import F5CcclError
from f5_ctlr_agent.gtm.infrastructure import GTMInfrastructure


@pytest.fixture
def mock_gtm():
    """Create a mock GTM object."""
    gtm = Mock()
    gtm.servers.server.exists = Mock(return_value=False)
    gtm.servers.server.create = Mock()
    gtm.servers.server.load = Mock()
    gtm.servers.server.get_collection = Mock(return_value=[])
    gtm.datacenters.datacenter.exists = Mock(return_value=True)
    gtm.datacenters.datacenter.load = Mock()
    return gtm


@pytest.fixture
def infrastructure_manager(mock_gtm):
    """Create GTMInfrastructure instance."""
    return GTMInfrastructure(mock_gtm, "Common")


class TestCreateGslbServer:
    """Tests for GTMInfrastructure.create_gslb_server()."""
    
    def test_create_new_server(self, infrastructure_manager, mock_gtm):
        """Test creating a new GSLB server."""
        mock_server = Mock()
        mock_gtm.servers.server.create.return_value = mock_server
        
        result = infrastructure_manager.create_gslb_server(
            "server-10-0-0-1",
            "dc1",
            ["10.0.0.1"],
            product="bigip",
            monitor="/Common/gateway_icmp"
        )
        
        mock_gtm.servers.server.create.assert_called_once()
        call_kwargs = mock_gtm.servers.server.create.call_args[1]
        assert call_kwargs['name'] == "server-10-0-0-1"
        assert call_kwargs['datacenter'] == "dc1"
        assert call_kwargs['product'] == "bigip"
        assert call_kwargs['monitor'] == "/Common/gateway_icmp"
        assert result == mock_server
    
    def test_create_server_already_exists(self, infrastructure_manager, mock_gtm):
        """Test creating server that already exists (loads existing)."""
        mock_gtm.servers.server.exists.return_value = True
        mock_server = Mock()
        mock_gtm.servers.server.load.return_value = mock_server
        
        result = infrastructure_manager.create_gslb_server(
            "server-10-0-0-1",
            "dc1",
            ["10.0.0.1"]
        )
        
        mock_gtm.servers.server.create.assert_not_called()
        mock_gtm.servers.server.load.assert_called_once()
        assert result == mock_server
    
    def test_create_server_with_dict_addresses(self, infrastructure_manager, mock_gtm):
        """Test creating server with addresses as dict format."""
        addresses = [{"name": "10.0.0.1", "translation": "none"}]
        
        infrastructure_manager.create_gslb_server(
            "server-10-0-0-1",
            "dc1",
            addresses
        )
        
        call_kwargs = mock_gtm.servers.server.create.call_args[1]
        assert call_kwargs['addresses'] == addresses


class TestCreateVirtualServer:
    """Tests for GTMInfrastructure.create_virtual_server_on_gslb_server()."""
    
    def test_create_new_vs(self, infrastructure_manager, mock_gtm):
        """Test creating new virtual server."""
        mock_server = Mock()
        mock_vs = Mock()
        mock_server.virtual_servers_s.virtual_server.create.return_value = mock_vs
        mock_gtm.servers.server.load.return_value = mock_server
        
        result = infrastructure_manager.create_virtual_server_on_gslb_server(
            "server-10-0-0-1",
            "vs-192-168-1-1-80",
            "192.168.1.1:80"
        )
        
        mock_server.virtual_servers_s.virtual_server.create.assert_called_once()
        call_kwargs = mock_server.virtual_servers_s.virtual_server.create.call_args[1]
        assert call_kwargs['name'] == "vs-192-168-1-1-80"
        assert call_kwargs['destination'] == "192.168.1.1:80"
        assert call_kwargs['enabled'] is True
        assert result == mock_vs
    
    def test_create_vs_with_pre_loaded_server(self, infrastructure_manager, mock_gtm):
        """Test creating VS with pre-loaded server object (optimization)."""
        mock_server = Mock()
        mock_vs = Mock()
        mock_server.virtual_servers_s.virtual_server.create.return_value = mock_vs
        
        result = infrastructure_manager.create_virtual_server_on_gslb_server(
            "server-10-0-0-1",
            "vs-192-168-1-1-80",
            "192.168.1.1:80",
            server_obj=mock_server
        )
        
        # Should NOT load server (use pre-loaded object)
        mock_gtm.servers.server.load.assert_not_called()
        assert result == mock_vs
    
    def test_create_vs_already_exists(self, infrastructure_manager, mock_gtm):
        """Test creating VS that already exists."""
        mock_server = Mock()
        mock_vs = Mock()
        mock_server.virtual_servers_s.virtual_server.create.side_effect = Exception("already exists")
        mock_server.virtual_servers_s.virtual_server.load.return_value = mock_vs
        
        result = infrastructure_manager.create_virtual_server_on_gslb_server(
            "server-10-0-0-1",
            "vs-192-168-1-1-80",
            "192.168.1.1:80",
            server_obj=mock_server
        )
        
        # Should load existing VS
        mock_server.virtual_servers_s.virtual_server.load.assert_called_once()
        assert result == mock_vs


class TestEnsureDatacenterExists:
    """Tests for GTMInfrastructure.ensure_datacenter_exists()."""
    
    def test_datacenter_exists(self, infrastructure_manager, mock_gtm):
        """Test when datacenter already exists."""
        mock_dc = Mock()
        mock_gtm.datacenters.datacenter.load.return_value = mock_dc
        
        result = infrastructure_manager.ensure_datacenter_exists("dc1")
        
        assert result == mock_dc
    
    def test_datacenter_does_not_exist(self, infrastructure_manager, mock_gtm):
        """Test when datacenter doesn't exist (should raise)."""
        mock_gtm.datacenters.datacenter.exists.return_value = False
        
        with pytest.raises(F5CcclError) as exc_info:
            infrastructure_manager.ensure_datacenter_exists("missing_dc")
        
        assert "does not exist" in str(exc_info.value)


class TestOrchestrateWithSnapshot:
    """Tests for GTMInfrastructure.orchestrate_with_snapshot()."""
    
    def test_orchestrate_empty_config(self, infrastructure_manager, mock_gtm):
        """Test orchestrating with empty configuration."""
        config = {"Common": {"dataCenter": "dc1"}}
        
        result = infrastructure_manager.orchestrate_with_snapshot(config, None)
        
        # Should return without creating anything
        assert isinstance(result, dict)
    
    def test_orchestrate_creates_servers(self, infrastructure_manager, mock_gtm):
        """Test orchestration creates necessary servers."""
        config = {
            "Common": {
                "dataCenter": "dc1",
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
        
        mock_server = Mock()
        mock_server.virtual_servers_s.get_collection.return_value = []
        mock_gtm.servers.server.create.return_value = mock_server
        
        result = infrastructure_manager.orchestrate_with_snapshot(config, None)
        
        # Should create server for DataServer 10.0.0.1
        mock_gtm.servers.server.create.assert_called()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
