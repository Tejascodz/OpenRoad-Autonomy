# OpenRoad-Autonomy 🚗🤖

<div align="center">
  
  ![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
  ![FastAPI](https://img.shields.io/badge/FastAPI-0.104-green.svg)
  ![License](https://img.shields.io/badge/License-MIT-yellow.svg)
  ![GitHub stars](https://img.shields.io/github/stars/Tejascodz/OpenRoad-Autonomy?style=social)
  
  <h3>A production-ready autonomous delivery robot system with real-time mapping, path planning, and simulation</h3>
  
  [Features](#features) • [Demo](#demo) • [Installation](#installation) • [Usage](#usage) • [Architecture](#architecture) • [API](#api) • [Contributing](#contributing)
  
</div>

---

## 📸 Demo & Screenshots

<div align="center">
  
  ### Live Robot Tracking Dashboard
  <!-- Add your dashboard screenshot URL here -->
  ![Dashboard](https://via.placeholder.com/800x400?text=OpenRoad-Autonomy+Dashboard)
  
  *Real-time robot tracking on interactive map with live updates*
  
  ### Path Planning Visualization
  <!-- Add your path planning screenshot URL here -->
  ![Path Planning](https://via.placeholder.com/800x400?text=A*+vs+Dijkstra+Path+Planning)
  
  *A* and Dijkstra algorithm comparison on real road networks*
  
  ### Robot State Management
  <!-- Add your robot state screenshot URL here -->
  ![Robot State](https://via.placeholder.com/800x400?text=Robot+State+Machine)
  
  *State machine with battery monitoring and obstacle detection*
  
</div>

## ✨ Features

<div align="center">
  
| 🌍 Mapping | 🗺️ Routing | 🤖 Simulation | 📊 Dashboard |
|------------|------------|---------------|--------------|
| Real OpenStreetMap | A* Algorithm | Battery Model | Live Tracking |
| Google Maps API | Dijkstra | Obstacle Detection | WebSocket Updates |
| Road Networks | Road Type Weights | State Machine | Delivery History |
| Fallback Grid | Distance Calculation | GPS Noise | Emergency Controls |

</div>

### Core Capabilities

- **🗺️ Real-world Mapping**: Uses OpenStreetMap for actual road networks with 26211+ nodes
- **🚀 Advanced Path Planning**: A* and Dijkstra algorithms with road type weighting
- **🤖 Realistic Robot Simulation**: Battery consumption, obstacle detection, state machine
- **📊 Live Dashboard**: Real-time robot tracking on interactive Leaflet map
- **🔋 Sophisticated Battery Model**: Consumption based on distance, speed, and grade
- **🗄️ Persistent Storage**: SQLite database for delivery history and path tracking
- **🌐 WebSocket Communication**: Live updates to frontend every second
- **🎯 Modular Design**: Easy integration with real hardware (GPS, LIDAR, motor control)

## 🏗️ System Architecture
┌─────────────────────────────────────────────────────────────────┐
│ OpenRoad-Autonomy System │
├─────────────────────────────────────────────────────────────────┤
│ │
│ ┌─────────────┐ ┌──────────────┐ ┌─────────────────┐ │
│ │ Frontend │────▶│ FastAPI │────▶│ Robot Controller│ │
│ │ (HTML/JS) │◀────│ Server │◀────│ │ │
│ └─────────────┘ └──────────────┘ └─────────────────┘ │
│ │ │ │ │
│ │ │ │ │
│ ▼ ▼ ▼ │
│ ┌─────────────┐ ┌──────────────┐ ┌─────────────────┐ │
│ │ Leaflet │ │ WebSocket │ │ SQLite │ │
│ │ Map │ │ Connections │ │ Database │ │
│ └─────────────┘ └──────────────┘ └─────────────────┘ │
│ │
└─────────────────────────────────────────────────────────────────┘

text

## 📁 Project Structure
OpenRoad-Autonomy/
├── 📂 app/
│ ├── 📄 init.py
│ ├── 📄 main.py # FastAPI application entry point
│ ├── 📄 config.py # Configuration management
│ ├── 📂 models/ # Database models
│ │ ├── 📄 delivery.py # Delivery schema
│ │ └── 📄 robot_state.py # Robot state machine
│ ├── 📂 services/ # Core services
│ │ ├── 📄 map_service.py # OSM/Google Maps integration
│ │ ├── 📄 routing_engine.py # A*/Dijkstra algorithms
│ │ ├── 📄 robot_controller.py # Robot control logic
│ │ ├── 📄 battery_model.py # Battery simulation
│ │ └── 📄 database_service.py # SQLite operations
│ ├── 📂 api/ # API endpoints
│ │ ├── 📄 routes.py # REST endpoints
│ │ └── 📄 websocket_manager.py # WebSocket handling
│ └── 📂 static/ # Frontend files
│ ├── 📄 index.html # Dashboard
│ ├── 📂 css/
│ │ └── 📄 style.css # Styling
│ └── 📂 js/
│ └── 📄 dashboard.js # Frontend logic
├── 📂 tests/ # Unit tests
├── 📂 logs/ # Application logs
├── 📂 data/ # SQLite database
├── 📄 requirements.txt # Python dependencies
├── 📄 .env.example # Environment variables
├── 📄 .gitignore # Git ignore rules
├── 📄 Dockerfile # Docker configuration
├── 📄 docker-compose.yml # Docker compose
└── 📄 README.md # This file

text

## 🚀 Quick Start

### Prerequisites

- Python 3.11 or higher
- pip package manager
- Git (optional, for cloning)

### Installation

```bash
# 1. Clone the repository
git clone https://github.com/Tejascodz/OpenRoad-Autonomy.git
cd OpenRoad-Autonomy

# 2. Create virtual environment
# On Windows:
python -m venv venv
venv\Scripts\activate
# On Mac/Linux:
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env with your configuration (optional)

# 5. Initialize database
python -c "from app.services.database_service import DatabaseService; DatabaseService()"

# 6. Run the application
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
Access the Application
Dashboard: http://localhost:8000/static/index.html

API Documentation: http://localhost:8000/docs

Health Check: http://localhost:8000/health

🎮 Usage
Sample Coordinates (Bangalore, India)
Location	Latitude	Longitude
Malleswaram (Pickup)	12.974178	77.545538
Rajajinagar (Delivery)	12.9082	77.5217
Majestic	12.9763	77.5712
Indiranagar	12.9719	77.6412
How to Use
Open the dashboard in your browser

Enter pickup and delivery coordinates

Select algorithm (A* or Dijkstra)

Click "Start Delivery"

Watch the robot navigate in real-time!

<div align="center">
https://via.placeholder.com/600x300?text=Click+Start+Delivery+to+Begin

</div>
📡 API Reference
REST Endpoints
Method	Endpoint	Description
POST	/api/v1/start_delivery	Start a new delivery mission
GET	/api/v1/robot_status	Get current robot status
POST	/api/v1/robot/emergency_stop	Emergency stop robot
POST	/api/v1/robot/resume	Resume mission
GET	/api/v1/deliveries/active	List active deliveries
GET	/api/v1/deliveries/history	Get delivery history
GET	/api/v1/deliveries/{id}	Get specific delivery
WebSocket
text
ws://localhost:8000/api/v1/ws/{robot_id}
Message Types:

ping - Keep connection alive

pong - Server response

request_state - Request robot state

state_update - Live robot updates

🧪 Testing
bash
# Run tests
pytest tests/

# Run with coverage
pytest --cov=app tests/
🐳 Docker Deployment
bash
# Build the image
docker build -t openroad-autonomy .

# Run the container
docker run -p 8000:8000 openroad-autonomy

# Or use docker-compose
docker-compose up -d
☁️ Cloud Deployment (AWS)
EC2 Deployment
bash
# SSH into EC2 instance
ssh -i your-key.pem ec2-user@your-instance-ip

# Install Docker
sudo yum update -y
sudo yum install docker -y
sudo service docker start
sudo usermod -a -G docker ec2-user

# Pull and run
docker pull tejascodz/openroad-autonomy
docker run -d -p 80:8000 tejascodz/openroad-autonomy
ECS Deployment
Push to Amazon ECR

Create ECS cluster

Configure task definition

Set up load balancer

Deploy service

🔧 Hardware Integration
The system is designed for easy integration with real hardware:

python
# Example hardware integration
class RealRobotHardware:
    def __init__(self):
        self.gps = GPSModule(port='/dev/ttyUSB0')
        self.lidar = RPLidar('/dev/ttyUSB1')
        self.motors = MotorController(pwm_pin=18)
    
    def read_gps(self):
        return self.gps.read()
    
    def scan_obstacles(self):
        return self.lidar.get_scan()
🤝 Contributing
Contributions are welcome! Please follow these steps:

Fork the repository

Create a feature branch (git checkout -b feature/AmazingFeature)

Commit changes (git commit -m 'Add AmazingFeature')

Push to branch (git push origin feature/AmazingFeature)

Open a Pull Request

📝 License
Distributed under the MIT License. See LICENSE for more information.

📧 Contact
Tejas - @Tejascodz

Project Link: https://github.com/Tejascodz/OpenRoad-Autonomy

🙏 Acknowledgments
OpenStreetMap for mapping data

FastAPI for the amazing framework

Leaflet.js for interactive maps

OSMnx for network analysis

<div align="center">
⭐ Star this repository if you find it useful!
Made with ❤️ for autonomous robotics

</div> ```
