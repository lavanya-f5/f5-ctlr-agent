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

"""Unit tests for GTMUtils - Pure utility functions."""

import pytest
from f5_ctlr_agent.gtm.utils import GTMUtils


class TestFormatServerName:
    """Tests for GTMUtils.format_server_name()."""
    
    def test_format_simple_ipv4(self):
        """Test formatting a simple IPv4 address."""
        result = GTMUtils.format_server_name("10.0.0.1")
        assert result == "server-10-0-0-1"
    
    def test_format_ipv4_with_leading_zeros(self):
        """Test IPv4 with leading zeros in octets."""
        result = GTMUtils.format_server_name("192.168.001.010")
        assert result == "server-192-168-001-010"
    
    def test_format_ipv6(self):
        """Test formatting IPv6 address."""
        result = GTMUtils.format_server_name("2001:db8::1")
        assert result == "server-2001-db8--1"
    
    def test_format_ipv6_full(self):
        """Test full IPv6 address."""
        result = GTMUtils.format_server_name("2001:0db8:0000:0000:0000:0000:0000:0001")
        assert result == "server-2001-0db8-0000-0000-0000-0000-0000-0001"


class TestFormatVsName:
    """Tests for GTMUtils.format_vs_name()."""
    
    def test_format_simple_destination(self):
        """Test formatting simple IP:PORT."""
        result = GTMUtils.format_vs_name("10.0.0.1:80")
        assert result == "vs-10-0-0-1-80"
    
    def test_format_with_dots_colons(self):
        """Test replacing dots and colons."""
        result = GTMUtils.format_vs_name("192.168.1.100:8080")
        assert result == "vs-192-168-1-100-8080"
    
    def test_format_with_percent(self):
        """Test replacing percent sign (route domain)."""
        result = GTMUtils.format_vs_name("10.0.0.1%2:443")
        assert result == "vs-10-0-0-1-2-443"
    
    def test_format_ipv6_destination(self):
        """Test IPv6 with port."""
        result = GTMUtils.format_vs_name("2001:db8::1.80")
        assert result == "vs-2001-db8--1-80"


class TestParseMemberSpec:
    """Tests for GTMUtils.parse_member_spec()."""
    
    def test_parse_three_part_member(self):
        """Test parsing DataServer|IP|Port format."""
        result = GTMUtils.parse_member_spec("10.0.0.1|192.168.1.1|80", None)
        assert result == ("10.0.0.1", "192.168.1.1", "80", "192.168.1.1:80")
    
    def test_parse_two_part_with_dataserver(self):
        """Test parsing IP:Port with pool_dataserver."""
        result = GTMUtils.parse_member_spec("192.168.1.1:80", "10.0.0.1")
        assert result == ("10.0.0.1", "192.168.1.1", "80", "192.168.1.1:80")
    
    def test_parse_two_part_without_dataserver(self):
        """Test parsing IP:Port without pool_dataserver (should return None)."""
        result = GTMUtils.parse_member_spec("192.168.1.1:80", None)
        assert result is None
    
    def test_parse_invalid_format(self):
        """Test parsing invalid format (single value)."""
        result = GTMUtils.parse_member_spec("invalid", None)
        assert result is None
    
    def test_parse_with_pipe_separator(self):
        """Test using pipe separator."""
        result = GTMUtils.parse_member_spec("10.0.0.1|192.168.1.1|443", None)
        assert result == ("10.0.0.1", "192.168.1.1", "443", "192.168.1.1:443")
    
    def test_parse_with_colon_separator(self):
        """Test using colon separator with dataserver."""
        result = GTMUtils.parse_member_spec("192.168.1.1:8080", "10.0.0.2")
        assert result == ("10.0.0.2", "192.168.1.1", "8080", "192.168.1.1:8080")


class TestConvertMemberToBigipReference:
    """Tests for GTMUtils.convert_member_to_bigip_reference()."""
    
    def test_convert_three_part_format(self):
        """Test converting DataServer|IP|Port to server:vs format."""
        result = GTMUtils.convert_member_to_bigip_reference("10.0.0.1|192.168.1.1|80", None)
        assert result == "server-10-0-0-1:vs-192-168-1-1-80"
    
    def test_convert_two_part_with_dataserver(self):
        """Test converting IP:Port with pool_dataserver."""
        result = GTMUtils.convert_member_to_bigip_reference("192.168.1.1:443", "10.0.0.1")
        assert result == "server-10-0-0-1:vs-192-168-1-1-443"
    
    def test_convert_already_in_correct_format(self):
        """Test member already in server:vs format (returns as-is)."""
        result = GTMUtils.convert_member_to_bigip_reference("server-10-0-0-1:vs-192-168-1-1-80", None)
        assert result == "server-10-0-0-1:vs-192-168-1-1-80"
    
    def test_convert_invalid_format_returns_original(self):
        """Test invalid format returns original string."""
        result = GTMUtils.convert_member_to_bigip_reference("invalid_member", None)
        assert result == "invalid_member"


class TestIsTransientError:
    """Tests for GTMUtils.is_transient_error()."""
    
    def test_timeout_error_is_transient(self):
        """Test timeout errors are transient."""
        error = Exception("Connection timeout occurred")
        assert GTMUtils.is_transient_error(error) is True
    
    def test_connection_error_is_transient(self):
        """Test connection errors are transient."""
        error = Exception("Connection reset by peer")
        assert GTMUtils.is_transient_error(error) is True
    
    def test_503_error_is_transient(self):
        """Test HTTP 503 is transient."""
        error = Exception("503 Service Unavailable")
        assert GTMUtils.is_transient_error(error) is True
    
    def test_500_error_is_transient(self):
        """Test HTTP 500 is transient."""
        error = Exception("HTTP Error 500: Internal Server Error")
        assert GTMUtils.is_transient_error(error) is True
    
    def test_404_error_is_permanent(self):
        """Test 404 is permanent (not transient)."""
        error = Exception("404 Not Found")
        assert GTMUtils.is_transient_error(error) is False
    
    def test_400_error_is_permanent(self):
        """Test 400 is permanent."""
        error = Exception("400 Bad Request")
        assert GTMUtils.is_transient_error(error) is False
    
    def test_validation_error_is_permanent(self):
        """Test validation errors are permanent."""
        error = Exception("Invalid configuration: missing required field")
        assert GTMUtils.is_transient_error(error) is False


class TestParseGtmConfigOnce:
    """Tests for GTMUtils.parse_gtm_config_once()."""
    
    def test_parse_simple_config(self):
        """Test parsing simple GTM configuration."""
        config = {
            "partition1": {
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
        
        result = GTMUtils.parse_gtm_config_once(config, "partition1")
        
        assert "dataservers" in result
        assert "10.0.0.1" in result["dataservers"]
        assert "vs_inventory" in result
        assert "all_member_refs" in result
        assert "all_server_names" in result
        assert "members_by_pool" in result
    
    def test_parse_empty_config(self):
        """Test parsing empty configuration."""
        config = {"partition1": {}}
        result = GTMUtils.parse_gtm_config_once(config, "partition1")
        
        assert result["dataservers"] == set()
        assert result["vs_inventory"] == {}
        assert result["all_member_refs"] == set()
    
    def test_parse_multiple_pools(self):
        """Test parsing config with multiple pools."""
        config = {
            "partition1": {
                "wideIPs": [
                    {
                        "name": "site1.com",
                        "pools": [
                            {
                                "name": "pool1",
                                "members": ["10.0.0.1|192.168.1.1|80", "10.0.0.1|192.168.1.2|80"]
                            }
                        ]
                    },
                    {
                        "name": "site2.com",
                        "pools": [
                            {
                                "name": "pool2",
                                "members": ["10.0.0.2|192.168.2.1|443"]
                            }
                        ]
                    }
                ]
            }
        }
        
        result = GTMUtils.parse_gtm_config_once(config, "partition1")
        
        assert len(result["dataservers"]) == 2
        assert "10.0.0.1" in result["dataservers"]
        assert "10.0.0.2" in result["dataservers"]
        assert len(result["all_member_refs"]) == 3


class TestProcessConfig:
    """Tests for GTMUtils.process_config()."""
    
    def test_process_new_wideip(self):
        """Test detecting new wideIP."""
        old_config = {"partition1": {"wideIPs": []}}
        new_config = {"partition1": {"wideIPs": [{"name": "new.com", "pools": []}]}}
        
        result = GTMUtils.process_config(old_config, new_config)
        
        assert len(result["wideIPs"]) == 1
        assert result["wideIPs"][0] == "new.com"
    
    def test_process_deleted_wideip(self):
        """Test detecting deleted wideIP."""
        old_config = {"partition1": {"wideIPs": [{"name": "old.com", "pools": []}]}}
        new_config = {"partition1": {"wideIPs": []}}
        
        result = GTMUtils.process_config(old_config, new_config)
        
        assert len(result["wideIPs"]) == 1
        assert result["wideIPs"][0] == "old.com"
    
    def test_process_no_changes(self):
        """Test when configs are identical."""
        config = {"partition1": {"wideIPs": [{"name": "same.com", "pools": []}]}}
        
        result = GTMUtils.process_config(config, config)
        
        assert result["wideIPs"] == []
        assert result["pools"] == []
        assert result["monitors"] == []


class TestCreateReverseMap:
    """Tests for GTMUtils.create_reverse_map()."""
    
    def test_create_reverse_map_simple(self):
        """Test creating reverse map for pools and monitors."""
        config = {
            "partition1": {
                "wideIPs": [
                    {
                        "name": "example.com",
                        "pools": [
                            {
                                "name": "pool1",
                                "monitors": [{"name": "http_mon"}]
                            }
                        ]
                    }
                ]
            }
        }
        
        result = GTMUtils.create_reverse_map(config)
        
        assert "pools" in result
        assert "pool1" in result["pools"]
        assert "example.com" in result["pools"]["pool1"]
        assert "monitors" in result
        assert "http_mon" in result["monitors"]
        assert result["monitors"]["http_mon"] == "pool1"
    
    def test_create_reverse_map_multiple_wideips(self):
        """Test reverse map with multiple wideIPs sharing pools."""
        config = {
            "partition1": {
                "wideIPs": [
                    {
                        "name": "site1.com",
                        "pools": [{"name": "shared_pool"}]
                    },
                    {
                        "name": "site2.com",
                        "pools": [{"name": "shared_pool"}]
                    }
                ]
            }
        }
        
        result = GTMUtils.create_reverse_map(config)
        
        assert len(result["pools"]["shared_pool"]) == 2
        assert "site1.com" in result["pools"]["shared_pool"]
        assert "site2.com" in result["pools"]["shared_pool"]


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
