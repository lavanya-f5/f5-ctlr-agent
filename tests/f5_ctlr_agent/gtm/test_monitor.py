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

"""Unit tests for GTMMonitor - Health monitor management."""

import pytest
from unittest.mock import Mock, MagicMock, call
from f5_cccl.exceptions import F5CcclError
from f5_ctlr_agent.gtm.monitor import GTMMonitor


@pytest.fixture
def mock_gtm():
    """Create a mock GTM object."""
    gtm = Mock()
    # HTTP monitors
    gtm.monitor.https.http.exists = Mock(return_value=False)
    gtm.monitor.https.http.create = Mock()
    gtm.monitor.https.http.load = Mock()
    # HTTPS monitors
    gtm.monitor.https_s.https.exists = Mock(return_value=False)
    gtm.monitor.https_s.https.create = Mock()
    gtm.monitor.https_s.https.load = Mock()
    # TCP monitors
    gtm.monitor.tcps.tcp.exists = Mock(return_value=False)
    gtm.monitor.tcps.tcp.create = Mock()
    gtm.monitor.tcps.tcp.load = Mock()
    return gtm


@pytest.fixture
def monitor_helper(mock_gtm):
    """Create GTMMonitor instance."""
    version_getter = Mock(return_value=16.1)
    return GTMMonitor(mock_gtm, "Common", bigip_version_getter=version_getter)


@pytest.fixture
def monitor_helper_old_version(mock_gtm):
    """Create GTMMonitor instance with old BIG-IP version."""
    version_getter = Mock(return_value=15.1)
    return GTMMonitor(mock_gtm, "Common", bigip_version_getter=version_getter)


class TestCreateMonitor:
    """Tests for GTMMonitor.create_monitor()."""
    
    def test_create_http_monitor(self, monitor_helper, mock_gtm):
        """Test creating HTTP monitor."""
        monitor = {
            "name": "http_mon",
            "type": "http",
            "send": "GET /health",
            "recv": "OK",
            "interval": 5,
            "timeout": 16
        }
        
        monitor_helper.create_monitor(monitor, "example.com")
        
        mock_gtm.monitor.https.http.create.assert_called_once_with(
            name="http_mon",
            partition="Common",
            send="GET /health",
            recv="OK",
            interval=5,
            timeout=16
        )
    
    def test_create_https_monitor_with_sni(self, monitor_helper, mock_gtm):
        """Test creating HTTPS monitor with SNI (BIG-IP 16.1+)."""
        monitor = {
            "name": "https_mon",
            "type": "https",
            "send": "GET /",
            "recv": "200",
            "interval": 10,
            "timeout": 30
        }
        
        monitor_helper.create_monitor(monitor, "example.com")
        
        mock_gtm.monitor.https_s.https.create.assert_called_once_with(
            name="https_mon",
            partition="Common",
            send="GET /",
            recv="200",
            sniServerName="example.com",
            interval=10,
            timeout=30
        )
    
    def test_create_https_monitor_without_sni(self, monitor_helper_old_version, mock_gtm):
        """Test creating HTTPS monitor without SNI (BIG-IP < 16.1)."""
        monitor = {
            "name": "https_mon",
            "type": "https",
            "send": "GET /",
            "recv": "200",
            "interval": 10,
            "timeout": 30
        }
        
        monitor_helper_old_version.create_monitor(monitor, "example.com")
        
        # Should NOT include sniServerName for old versions
        mock_gtm.monitor.https_s.https.create.assert_called_once()
        call_kwargs = mock_gtm.monitor.https_s.https.create.call_args[1]
        assert "sniServerName" not in call_kwargs
    
    def test_create_tcp_monitor(self, monitor_helper, mock_gtm):
        """Test creating TCP monitor."""
        monitor = {
            "name": "tcp_mon",
            "type": "tcp",
            "interval": 5,
            "timeout": 16
        }
        
        monitor_helper.create_monitor(monitor, "example.com")
        
        mock_gtm.monitor.tcps.tcp.create.assert_called_once_with(
            name="tcp_mon",
            partition="Common",
            interval=5,
            timeout=16
        )
    
    def test_update_existing_http_monitor(self, monitor_helper, mock_gtm):
        """Test updating existing HTTP monitor."""
        mock_gtm.monitor.https.http.exists.return_value = True
        mock_monitor = Mock()
        mock_gtm.monitor.https.http.load.return_value = mock_monitor
        
        monitor = {
            "name": "http_mon",
            "type": "http",
            "send": "GET /updated",
            "recv": "SUCCESS",
            "interval": 10,
            "timeout": 20
        }
        
        monitor_helper.create_monitor(monitor, "example.com")
        
        assert mock_monitor.send == "GET /updated"
        assert mock_monitor.recv == "SUCCESS"
        assert mock_monitor.interval == 10
        assert mock_monitor.timeout == 20
        mock_monitor.update.assert_called_once()
    
    def test_create_empty_monitor(self, monitor_helper, mock_gtm):
        """Test creating with empty/None monitor (should do nothing)."""
        monitor_helper.create_monitor(None, "example.com")
        
        mock_gtm.monitor.https.http.create.assert_not_called()
        mock_gtm.monitor.https_s.https.create.assert_not_called()
        mock_gtm.monitor.tcps.tcp.create.assert_not_called()


class TestDeleteMonitor:
    """Tests for GTMMonitor.delete_monitor()."""
    
    def test_delete_http_monitor(self, monitor_helper, mock_gtm):
        """Test deleting HTTP monitor."""
        mock_monitor = Mock()
        mock_gtm.monitor.https.http.load.return_value = mock_monitor
        
        monitor_helper.delete_monitor("http_mon", "http")
        
        mock_monitor.delete.assert_called_once()
    
    def test_delete_https_monitor(self, monitor_helper, mock_gtm):
        """Test deleting HTTPS monitor."""
        mock_monitor = Mock()
        mock_gtm.monitor.https_s.https.load.return_value = mock_monitor
        
        monitor_helper.delete_monitor("https_mon", "https")
        
        mock_monitor.delete.assert_called_once()
    
    def test_delete_tcp_monitor(self, monitor_helper, mock_gtm):
        """Test deleting TCP monitor."""
        mock_monitor = Mock()
        mock_gtm.monitor.tcps.tcp.load.return_value = mock_monitor
        
        monitor_helper.delete_monitor("tcp_mon", "tcp")
        
        mock_monitor.delete.assert_called_once()
    
    def test_delete_already_deleted_monitor(self, monitor_helper, mock_gtm):
        """Test deleting monitor that's already gone (404)."""
        mock_gtm.monitor.https.http.load.side_effect = Exception("404 Not Found")
        
        # Should not raise - treat 404 as success
        monitor_helper.delete_monitor("http_mon", "http")


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
