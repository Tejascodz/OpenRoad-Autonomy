import osmnx as ox
import math
import networkx as nx
import googlemaps
from typing import Tuple, List, Dict, Optional
import numpy as np
from shapely.geometry import Point, LineString
import logging
from ..config import settings, logger

class MapService:
    """Production map service with fallback between OSM and Google Maps"""
    
    def __init__(self, use_google_maps: bool = False):
        self.use_google_maps = use_google_maps and settings.GOOGLE_MAPS_API_KEY
        self.graph = None
        self.graph_boundary = None
        self.default_lat = 12.9716  # Bangalore latitude
        self.default_lon = 77.5946  # Bangalore longitude
        
        if self.use_google_maps:
            self.gmaps = googlemaps.Client(key=settings.GOOGLE_MAPS_API_KEY)
            logger.info("Initialized Google Maps client")
        else:
            # Configure OSMnx
            try:
                ox.settings.log_console = False
                ox.settings.use_cache = True
                ox.settings.cache_folder = './data/cache'
                logger.info("Initialized OpenStreetMap client")
                
                # Initialize graph with default location
                self.fetch_road_network(self.default_lat, self.default_lon, radius_m=5000)
                logger.info("Successfully initialized road network at startup")
            except Exception as e:
                logger.error(f"Failed to initialize road network at startup: {str(e)}")
                logger.warning("Graph will be created on first request")
    
    def fetch_road_network(self, center_lat: float, center_lon: float, 
                          radius_m: int = 5000) -> nx.MultiDiGraph:
        """Fetch road network around a center point"""
        try:
            if self.use_google_maps:
                logger.info("Using Google Maps for routing")
                return self._create_graph_from_google(center_lat, center_lon, radius_m)
            else:
                logger.info(f"Fetching OSM network around ({center_lat}, {center_lon})")
                self.graph = ox.graph_from_point(
                    (center_lat, center_lon), 
                    dist=radius_m,
                    network_type='drive',
                    simplify=True
                )
                self.graph_boundary = (center_lat, center_lon, radius_m)
                logger.info(f"Fetched graph with {len(self.graph.nodes)} nodes and {len(self.graph.edges)} edges")
                return self.graph
        except Exception as e:
            logger.error(f"Failed to fetch road network: {str(e)}")
            logger.warning("Creating fallback demo graph")
            return self._create_fallback_graph(center_lat, center_lon, radius_m)
    
    def _create_fallback_graph(self, center_lat: float, center_lon: float, 
                              radius_m: int) -> nx.MultiDiGraph:
        """Create a fallback grid graph when OSMnx fails"""
        G = nx.MultiDiGraph()
        
        # Create a simple grid
        grid_size = 10
        step = 0.001  # ~111 meters
        
        for i in range(grid_size):
            for j in range(grid_size):
                node_id = f"node_{i}_{j}"
                lat = center_lat + (i - grid_size/2) * step
                lon = center_lon + (j - grid_size/2) * step
                G.add_node(node_id, y=lat, x=lon, street_count=4)
        
        # Add edges
        for i in range(grid_size):
            for j in range(grid_size):
                # Connect to right neighbor
                if i < grid_size - 1:
                    node1 = f"node_{i}_{j}"
                    node2 = f"node_{i+1}_{j}"
                    dist = self._haversine_distance(
                        (G.nodes[node1]['y'], G.nodes[node1]['x']),
                        (G.nodes[node2]['y'], G.nodes[node2]['x'])
                    )
                    G.add_edge(node1, node2, length=dist, highway='residential')
                    G.add_edge(node2, node1, length=dist, highway='residential')
                
                # Connect to bottom neighbor
                if j < grid_size - 1:
                    node1 = f"node_{i}_{j}"
                    node2 = f"node_{i}_{j+1}"
                    dist = self._haversine_distance(
                        (G.nodes[node1]['y'], G.nodes[node1]['x']),
                        (G.nodes[node2]['y'], G.nodes[node2]['x'])
                    )
                    G.add_edge(node1, node2, length=dist, highway='residential')
                    G.add_edge(node2, node1, length=dist, highway='residential')
        
        self.graph = G
        logger.info(f"Created fallback graph with {len(G.nodes)} nodes and {len(G.edges)} edges")
        return G
    
    def _haversine_distance(self, point1: Tuple[float, float], point2: Tuple[float, float]) -> float:
        """Calculate distance between two points in meters"""
        lat1, lon1 = point1
        lat2, lon2 = point2
        R = 6371000  # Earth's radius in meters
        
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)
        
        a = math.sin(delta_phi / 2) ** 2 + \
            math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        
        return R * c
    
    def _create_graph_from_google(self, center_lat: float, center_lon: float, 
                                  radius_m: int) -> nx.MultiDiGraph:
        """Create a graph using Google Maps Directions API"""
        G = nx.MultiDiGraph()
        
        # Add a central node
        center_node = f"node_0"
        G.add_node(center_node, y=center_lat, x=center_lon, street_count=0)
        
        try:
            # Fetch major roads from Google Places API
            places = self.gmaps.places_nearby(
                location=(center_lat, center_lon),
                radius=radius_m,
                type='route'
            )
            logger.info(f"Created Google Maps graph with {len(G.nodes)} nodes")
        except Exception as e:
            logger.error(f"Google Places API error: {str(e)}")
        
        return G
    
    def get_route(self, start_lat: float, start_lon: float, 
                 end_lat: float, end_lon: float,
                 algorithm: str = 'astar') -> Tuple[List[Tuple[float, float]], float, float]:
        """Get route between two points using specified algorithm"""
        logger.info(f"=== GET ROUTE CALLED ===")
        logger.info(f"Start: {start_lat}, {start_lon}")
        logger.info(f"End: {end_lat}, {end_lon}")
        logger.info(f"Algorithm: {algorithm}")
        
        try:
            if self.use_google_maps:
                return self._google_route(start_lat, start_lon, end_lat, end_lon)
            else:
                return self._osm_route(start_lat, start_lon, end_lat, end_lon, algorithm)
        except Exception as e:
            logger.error(f"Route calculation failed: {str(e)}", exc_info=True)
            logger.warning("Returning straight line fallback route")
            return self._get_straight_line_route(start_lat, start_lon, end_lat, end_lon)
    
    def _osm_route(self, start_lat: float, start_lon: float,
                   end_lat: float, end_lon: float,
                   algorithm: str) -> Tuple[List[Tuple[float, float]], float, float]:
        """Route using OSMnx with specified algorithm"""
        if not self.graph:
            logger.info("No graph found, fetching road network...")
            self.fetch_road_network(start_lat, start_lon)
        
        try:
            # Find nearest nodes
            logger.info("Finding nearest nodes...")
            start_node = ox.distance.nearest_nodes(self.graph, start_lon, start_lat)
            end_node = ox.distance.nearest_nodes(self.graph, end_lon, end_lat)
            
            logger.info(f"Routing from node {start_node} to {end_node}")
            
            # Calculate route based on algorithm
            if algorithm == 'dijkstra':
                route = nx.shortest_path(self.graph, start_node, end_node, weight='length')
            else:  # A* with heuristic
                def heuristic(u, v):
                    # Euclidean distance heuristic using great circle distance
                    u_data = self.graph.nodes[u]
                    v_data = self.graph.nodes[v]
                    # Calculate great circle distance in meters
                    return self._haversine_distance(
                        (u_data['y'], u_data['x']),
                        (v_data['y'], v_data['x'])
                    )
                
                route = nx.astar_path(self.graph, start_node, end_node, 
                                     heuristic=heuristic, weight='length')
            
            # Extract coordinates and calculate total distance
            route_coords = []
            total_distance = 0
            
            for i in range(len(route) - 1):
                u, v = route[i], route[i + 1]
                edge_data = self.graph.get_edge_data(u, v)[0]
                segment_distance = edge_data['length']
                total_distance += segment_distance
                
                # Add intermediate points for smooth animation
                u_coord = (self.graph.nodes[u]['y'], self.graph.nodes[u]['x'])
                v_coord = (self.graph.nodes[v]['y'], self.graph.nodes[v]['x'])
                
                if i == 0:
                    route_coords.append(u_coord)
                
                # Add points along the edge for smooth movement
                line = LineString([(u_coord[1], u_coord[0]), (v_coord[1], v_coord[0])])
                num_points = max(2, int(segment_distance / 10))  # One point every ~10m
                for j in range(1, num_points):
                    point = line.interpolate(j / num_points, normalized=True)
                    route_coords.append((point.y, point.x))
                
                route_coords.append(v_coord)
            
            # Calculate duration based on robot speed
            duration_min = (total_distance / 1000) / settings.ROBOT_SPEED_KMH * 60
            
            logger.info(f"Route calculated: {total_distance:.2f}m, {duration_min:.2f}min")
            
            return route_coords, total_distance, duration_min
            
        except Exception as e:
            logger.error(f"OSM routing failed: {str(e)}", exc_info=True)
            raise
    
    def _google_route(self, start_lat: float, start_lon: float,
                      end_lat: float, end_lon: float) -> Tuple[List[Tuple[float, float]], float, float]:
        """Route using Google Maps Directions API"""
        try:
            directions = self.gmaps.directions(
                (start_lat, start_lon),
                (end_lat, end_lon),
                mode="driving",
                departure_time="now"
            )
            
            if not directions:
                raise ValueError("No route found")
            
            route = directions[0]
            
            # Extract polyline and decode
            polyline = route['overview_polyline']['points']
            coords = googlemaps.convert.decode_polyline(polyline)
            route_coords = [(point['lat'], point['lng']) for point in coords]
            
            # Calculate total distance and duration
            total_distance = sum(leg['distance']['value'] for leg in route['legs'])
            total_duration = sum(leg['duration']['value'] for leg in route['legs']) / 60
            
            logger.info(f"Google route: {total_distance}m, {total_duration}min")
            
            return route_coords, total_distance, total_duration
            
        except Exception as e:
            logger.error(f"Google routing failed: {str(e)}", exc_info=True)
            raise
    
    def _get_straight_line_route(self, start_lat, start_lon, end_lat, end_lon):
        """Return a straight line route as fallback"""
        logger.info("Creating straight line fallback route")
        coords = []
        steps = 20
        
        for i in range(steps + 1):
            t = i / steps
            lat = start_lat + (end_lat - start_lat) * t
            lon = start_lon + (end_lon - start_lon) * t
            coords.append((lat, lon))
        
        distance = self._haversine_distance((start_lat, start_lon), (end_lat, end_lon))
        duration_min = (distance / 1000) / settings.ROBOT_SPEED_KMH * 60
        
        logger.info(f"Fallback route: {distance:.2f}m, {duration_min:.2f}min")
        
        return coords, distance, duration_min