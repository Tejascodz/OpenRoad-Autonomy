// Global variables
let map;
let robotMarker;
let pathLine;
let pickupMarker;
let deliveryMarker;
let robotPath = [];
let socket;
let robotId = 'R001';
let updateInterval;
let followRobot = true;  // Track if we should follow the robot
let lastRobotPosition = null;

// Initialize map
function initMap(centerLat = 12.9716, centerLon = 77.5946) {
    map = L.map('map', {
        center: [centerLat, centerLon],
        zoom: 14,
        zoomControl: true,
        fadeAnimation: true,
        markerZoomAnimation: true
    });
    
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '© OpenStreetMap contributors',
        maxZoom: 19
    }).addTo(map);
    
    // Add custom robot marker with pulsing effect
    const robotIcon = L.divIcon({
        className: 'custom-robot-marker',
        html: '<div class="robot-pulse"></div><div class="robot-icon">🤖</div>',
        iconSize: [40, 40],
        popupAnchor: [0, -20]
    });
    
    robotMarker = L.marker([centerLat, centerLon], { 
        icon: robotIcon,
        zIndexOffset: 1000  // Keep robot on top
    }).addTo(map);
    
    // Add a circle around robot to show range
    const robotCircle = L.circle([centerLat, centerLon], {
        color: '#3498db',
        fillColor: '#3498db',
        fillOpacity: 0.1,
        radius: 50,
        weight: 2
    }).addTo(map);
    
    robotMarker.relatedCircle = robotCircle;
    
    // Add map click handler to toggle follow mode
    map.on('dragstart', function() {
        followRobot = false;
        showNotification('Map tracking disabled - Drag to recenter or click "Re-center" button', 'info');
    });
    
    // Add re-center button
    const recenterButton = L.control({position: 'topright'});
    recenterButton.onAdd = function(map) {
        const div = L.DomUtil.create('div', 'recenter-button');
        div.innerHTML = '<button style="background: white; border: 2px solid #3498db; border-radius: 5px; padding: 8px 15px; cursor: pointer; font-weight: bold; box-shadow: 0 2px 5px rgba(0,0,0,0.2);">🎯 Re-center on Robot</button>';
        div.onclick = function() {
            followRobot = true;
            if (lastRobotPosition) {
                map.setView([lastRobotPosition.lat, lastRobotPosition.lon], map.getZoom());
            }
            showNotification('Now following robot', 'success');
        };
        return div;
    };
    recenterButton.addTo(map);
    
    // Initialize WebSocket connection
    connectWebSocket();
    
    // Fetch initial robot state
    fetchRobotStatus();
    
    // Fetch delivery history
    fetchDeliveryHistory();
    
    // Start periodic updates
    updateInterval = setInterval(fetchRobotStatus, 2000);
}

// WebSocket connection
function connectWebSocket() {
    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsProtocol}//${window.location.host}/api/v1/ws/${robotId}`;
    
    socket = new WebSocket(wsUrl);
    
    socket.onopen = function() {
        document.getElementById('connection-status').innerHTML = '🟢 Connected';
        document.getElementById('connection-status').style.color = '#00C851';
        // Send ping every 30 seconds to keep connection alive
        setInterval(() => {
            if (socket.readyState === WebSocket.OPEN) {
                socket.send(JSON.stringify({ type: 'ping' }));
            }
        }, 30000);
    };
    
    socket.onmessage = function(event) {
        const data = JSON.parse(event.data);
        if (data.type === 'robot_update') {
            updateDashboard(data.data);
        } else if (data.type === 'pong') {
            console.log('Connection alive');
        }
    };
    
    socket.onclose = function() {
        document.getElementById('connection-status').innerHTML = '🔴 Disconnected';
        document.getElementById('connection-status').style.color = '#ff4444';
        // Attempt to reconnect after 5 seconds
        setTimeout(connectWebSocket, 5000);
    };
    
    socket.onerror = function(error) {
        console.error('WebSocket error:', error);
    };
}

// Fetch robot status via REST API
async function fetchRobotStatus() {
    try {
        const response = await fetch('/api/v1/robot_status');
        const data = await response.json();
        updateDashboard(data);
    } catch (error) {
        console.error('Error fetching robot status:', error);
    }
}

// Update dashboard with robot data
function updateDashboard(data) {
    lastRobotPosition = data.position;
    
    // Update robot marker and circle
    if (data.position) {
        robotMarker.setLatLng([data.position.lat, data.position.lon]);
        if (robotMarker.relatedCircle) {
            robotMarker.relatedCircle.setLatLng([data.position.lat, data.position.lon]);
        }
        
        // Only center map if follow mode is enabled
        if (followRobot) {
            map.panTo([data.position.lat, data.position.lon], {animate: true, duration: 0.5});
        }
        
        document.getElementById('position').textContent = 
            `${data.position.lat.toFixed(6)}, ${data.position.lon.toFixed(6)}`;
    }
    
    // Update battery
    const batteryPercent = data.battery || 0;
    document.getElementById('battery-fill').style.width = `${batteryPercent}%`;
    document.getElementById('battery-percent').textContent = `${batteryPercent.toFixed(1)}%`;
    
    // Set color based on battery level
    const batteryFill = document.getElementById('battery-fill');
    if (batteryPercent < 20) {
        batteryFill.style.backgroundColor = '#ff4444';
    } else if (batteryPercent < 50) {
        batteryFill.style.backgroundColor = '#ffaa00';
    } else {
        batteryFill.style.backgroundColor = '#00C851';
    }
    
    // Update other stats
    document.getElementById('robot-mode').textContent = `Mode: ${data.mode.toUpperCase()}`;
    document.getElementById('speed').textContent = `${data.speed.toFixed(1)} km/h`;
    document.getElementById('delivery-id').textContent = data.delivery_id || '-';
    
    // Update progress
    const progress = data.progress || 0;
    document.getElementById('progress-fill').style.width = `${progress}%`;
    document.getElementById('progress-percent').textContent = `${progress.toFixed(1)}%`;
    
    // Handle obstacle warning
    if (data.obstacle_detected) {
        showNotification('⚠️ Obstacle detected!', 'warning');
    }
}

// Start new delivery
document.getElementById('delivery-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    
    const pickupLat = parseFloat(document.getElementById('pickup-lat').value);
    const pickupLon = parseFloat(document.getElementById('pickup-lon').value);
    const deliveryLat = parseFloat(document.getElementById('delivery-lat').value);
    const deliveryLon = parseFloat(document.getElementById('delivery-lon').value);
    const algorithm = document.getElementById('algorithm').value;
    
    if (isNaN(pickupLat) || isNaN(pickupLon) || isNaN(deliveryLat) || isNaN(deliveryLon)) {
        showNotification('Please enter valid coordinates', 'error');
        return;
    }
    
    try {
        const response = await fetch('/api/v1/start_delivery', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                pickup_lat: pickupLat,
                pickup_lon: pickupLon,
                delivery_lat: deliveryLat,
                delivery_lon: deliveryLon,
                algorithm: algorithm
            })
        });
        
        const data = await response.json();
        
        if (data.success) {
            showNotification(`Delivery started! ID: ${data.delivery_id}`, 'success');
            // Clear form
            document.getElementById('delivery-form').reset();
            // Fetch path and draw it
            fetchDeliveryPath(data.delivery_id);
            // Enable follow mode
            followRobot = true;
        } else {
            showNotification(`Error: ${data.message}`, 'error');
        }
    } catch (error) {
        console.error('Error starting delivery:', error);
        showNotification('Failed to start delivery', 'error');
    }
});

// Fetch and draw delivery path
async function fetchDeliveryPath(deliveryId) {
    try {
        const response = await fetch(`/api/v1/deliveries/${deliveryId}`);
        const data = await response.json();
        
        if (data.path_planned) {
            const path = JSON.parse(data.path_planned);
            
            // Clear existing markers and path
            if (pathLine) {
                map.removeLayer(pathLine);
            }
            if (pickupMarker) {
                map.removeLayer(pickupMarker);
            }
            if (deliveryMarker) {
                map.removeLayer(deliveryMarker);
            }
            
            // Draw path with better visibility
            pathLine = L.polyline(path, { 
                color: '#FF6B6B',  // Bright red
                weight: 5,
                opacity: 0.8,
                lineCap: 'round',
                lineJoin: 'round',
                dashArray: null,
                smoothFactor: 1
            }).addTo(map);
            
            // Add arrow markers to show direction
            const arrowIcon = L.divIcon({
                className: 'arrow-marker',
                html: '▶',
                iconSize: [15, 15]
            });
            
            // Add directional arrows every few points
            for (let i = 0; i < path.length - 1; i += 5) {
                const midPoint = [
                    (path[i][0] + path[i+1][0]) / 2,
                    (path[i][1] + path[i+1][1]) / 2
                ];
                L.marker(midPoint, { 
                    icon: arrowIcon,
                    rotationAngle: calculateBearing(path[i], path[i+1])
                }).addTo(map);
            }
            
            // Add pickup marker (green)
            pickupMarker = L.marker([data.pickup.lat, data.pickup.lon], {
                icon: L.divIcon({
                    className: 'pickup-marker',
                    html: '<div style="background: #00C851; width: 20px; height: 20px; border-radius: 50%; border: 3px solid white; box-shadow: 0 2px 5px rgba(0,0,0,0.3);"></div>',
                    iconSize: [26, 26]
                })
            }).bindPopup('📍 Pickup Location').addTo(map);
            
            // Add delivery marker (red)
            deliveryMarker = L.marker([data.delivery.lat, data.delivery.lon], {
                icon: L.divIcon({
                    className: 'delivery-marker',
                    html: '<div style="background: #ff4444; width: 20px; height: 20px; border-radius: 50%; border: 3px solid white; box-shadow: 0 2px 5px rgba(0,0,0,0.3);"></div>',
                    iconSize: [26, 26]
                })
            }).bindPopup('📦 Delivery Location').addTo(map);
            
            // Add labels
            pickupMarker.bindTooltip('PICKUP', {permanent: false, direction: 'top'});
            deliveryMarker.bindTooltip('DELIVERY', {permanent: false, direction: 'top'});
            
            // Fit map to show entire route with padding
            const bounds = L.latLngBounds(path);
            map.fitBounds(bounds, { padding: [50, 50] });
            
            // Reset follow mode after fitting bounds
            followRobot = true;
        }
    } catch (error) {
        console.error('Error fetching path:', error);
    }
}

// Calculate bearing between two points for arrow direction
function calculateBearing(point1, point2) {
    const lat1 = point1[0] * Math.PI / 180;
    const lat2 = point2[0] * Math.PI / 180;
    const lon1 = point1[1] * Math.PI / 180;
    const lon2 = point2[1] * Math.PI / 180;
    
    const y = Math.sin(lon2 - lon1) * Math.cos(lat2);
    const x = Math.cos(lat1) * Math.sin(lat2) -
              Math.sin(lat1) * Math.cos(lat2) * Math.cos(lon2 - lon1);
    const bearing = Math.atan2(y, x) * 180 / Math.PI;
    return (bearing + 360) % 360;
}

// Fetch delivery history
async function fetchDeliveryHistory() {
    try {
        const response = await fetch('/api/v1/deliveries/history?limit=10');
        const deliveries = await response.json();
        
        const tbody = document.getElementById('history-body');
        tbody.innerHTML = '';
        
        deliveries.forEach(delivery => {
            const row = tbody.insertRow();
            row.innerHTML = `
                <td>${delivery.id}</td>
                <td>${delivery.pickup.lat.toFixed(4)}, ${delivery.pickup.lon.toFixed(4)}</td>
                <td>${delivery.delivery.lat.toFixed(4)}, ${delivery.delivery.lon.toFixed(4)}</td>
                <td>${(delivery.total_distance_m / 1000).toFixed(2)} km</td>
                <td><span class="status-${delivery.status}">${delivery.status}</span></td>
                <td>${new Date(delivery.created_at).toLocaleTimeString()}</td>
            `;
        });
    } catch (error) {
        console.error('Error fetching history:', error);
    }
}

// Emergency stop
document.getElementById('emergency-stop').addEventListener('click', async () => {
    if (confirm('Are you sure you want to emergency stop the robot?')) {
        try {
            const response = await fetch('/api/v1/robot/emergency_stop', {
                method: 'POST'
            });
            const data = await response.json();
            showNotification(data.message, 'warning');
        } catch (error) {
            console.error('Error stopping robot:', error);
        }
    }
});

// Resume mission
document.getElementById('resume-mission').addEventListener('click', async () => {
    try {
        const response = await fetch('/api/v1/robot/resume', {
            method: 'POST'
        });
        const data = await response.json();
        showNotification(data.message, 'success');
    } catch (error) {
        console.error('Error resuming mission:', error);
    }
});

// Notification system
function showNotification(message, type) {
    const notification = document.createElement('div');
    notification.className = `notification ${type}`;
    notification.textContent = message;
    notification.style.cssText = `
        position: fixed;
        top: 20px;
        right: 20px;
        padding: 15px 20px;
        background: ${type === 'success' ? '#00C851' : type === 'warning' ? '#ffbb33' : '#ff4444'};
        color: white;
        border-radius: 5px;
        z-index: 9999;
        animation: slideIn 0.3s ease;
        box-shadow: 0 4px 15px rgba(0,0,0,0.2);
        font-weight: bold;
    `;
    
    document.body.appendChild(notification);
    
    setTimeout(() => {
        notification.style.animation = 'slideOut 0.3s ease';
        setTimeout(() => notification.remove(), 300);
    }, 3000);
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    // Try to get user's location for initial map view
    if (navigator.geolocation) {
        navigator.geolocation.getCurrentPosition(
            (position) => {
                initMap(position.coords.latitude, position.coords.longitude);
            },
            () => {
                initMap(); // Use default (Bangalore)
            }
        );
    } else {
        initMap(); // Use default (Bangalore)
    }
});