import networkx as nx
import numpy as np
from typing import List, Tuple, Dict, Optional
from enum import Enum
import logging
from ..config import logger

class RoadType(Enum):
    HIGHWAY = 1.0
    MAIN_ROAD = 1.2
    RESIDENTIAL = 1.5
    SERVICE = 2.0
    PEDESTRIAN = 3.0

class RoutingEngine:
    """Advanced routing engine with A* and Dijkstra comparison"""
    
    def __init__(self, graph):
        self.graph = graph
        self.road_type_weights = {
            'motorway': RoadType.HIGHWAY.value,
            'trunk': RoadType.HIGHWAY.value,
            'primary': RoadType.MAIN_ROAD.value,
            'secondary': RoadType.MAIN_ROAD.value,
            'tertiary': RoadType.RESIDENTIAL.value,
            'residential': RoadType.RESIDENTIAL.value,
            'service': RoadType.SERVICE.value,
            'unclassified': RoadType.SERVICE.value,
            'path': RoadType.PEDESTRIAN.value,
            'footway': RoadType.PEDESTRIAN.value,
        }
    
    def calculate_route_astar(self, start_node: int, end_node: int) -> Tuple[List[int], float]:
        """A* pathfinding with road type weights"""
        start_time = __import__('time').time()
        
        def heuristic(u, v):
            # Geographic distance heuristic
            u_data = self.graph.nodes[u]
            v_data = self.graph.nodes[v]
            return np.sqrt((u_data['x'] - v_data['x'])**2 + 
                          (u_data['y'] - v_data['y'])**2) * 111000  # Approx meters
        
        def weight(u, v, d):
            # Apply road type weights to actual distance
            base_distance = d['length']
            road_type = d.get('highway', 'residential')
            if isinstance(road_type, list):
                road_type = road_type[0]
            weight_factor = self.road_type_weights.get(road_type, RoadType.RESIDENTIAL.value)
            return base_distance * weight_factor
        
        try:
            path = nx.astar_path(self.graph, start_node, end_node, 
                                heuristic=heuristic, weight=weight)
            
            # Calculate actual distance
            distance = 0
            for i in range(len(path) - 1):
                edge_data = self.graph.get_edge_data(path[i], path[i + 1])[0]
                distance += edge_data['length']
            
            computation_time = (__import__('time').time() - start_time) * 1000
            logger.info(f"A* completed in {computation_time:.2f}ms, distance: {distance:.2f}m")
            
            return path, distance
        except Exception as e:
            logger.error(f"A* routing failed: {str(e)}")
            raise
    
    def calculate_route_dijkstra(self, start_node: int, end_node: int) -> Tuple[List[int], float]:
        """Dijkstra pathfinding for comparison"""
        start_time = __import__('time').time()
        
        def weight(u, v, d):
            return d['length']  # Dijkstra uses actual distance
        
        try:
            path = nx.shortest_path(self.graph, start_node, end_node, weight=weight)
            
            distance = 0
            for i in range(len(path) - 1):
                edge_data = self.graph.get_edge_data(path[i], path[i + 1])[0]
                distance += edge_data['length']
            
            computation_time = (__import__('time').time() - start_time) * 1000
            logger.info(f"Dijkstra completed in {computation_time:.2f}ms, distance: {distance:.2f}m")
            
            return path, distance
        except Exception as e:
            logger.error(f"Dijkstra routing failed: {str(e)}")
            raise
    
    def compare_algorithms(self, start_node: int, end_node: int) -> Dict:
        """Compare A* and Dijkstra performance"""
        astar_path, astar_distance = self.calculate_route_astar(start_node, end_node)
        dijkstra_path, dijkstra_distance = self.calculate_route_dijkstra(start_node, end_node)
        
        # In most cases, distances should be similar but paths might differ
        # due to road type weighting in A*
        
        return {
            'astar': {
                'path': astar_path,
                'distance_m': astar_distance,
                'path_length': len(astar_path)
            },
            'dijkstra': {
                'path': dijkstra_path,
                'distance_m': dijkstra_distance,
                'path_length': len(dijkstra_path)
            },
            'distance_difference': abs(astar_distance - dijkstra_distance),
            'recommended': 'astar' if astar_distance <= dijkstra_distance else 'dijkstra'
        }