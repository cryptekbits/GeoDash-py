"""
City data management module for the GeoDash package.

This module provides the main CityData class that serves as a facade for accessing
city data through various repositories.
"""

import os
from typing import Dict, List, Any, Optional, Union, Tuple, TypeVar, Iterator, Type, cast, overload, Set, Callable, Protocol
from functools import lru_cache

from GeoDash.data.database import DatabaseManager
from GeoDash.data.schema import SchemaManager
from GeoDash.data.importer import CityDataImporter, get_data_directory
from GeoDash.data.repositories import (
    get_city_repository, 
    get_geo_repository, 
    get_region_repository
)
from GeoDash.utils.logging import get_logger

# Get a logger for this module
logger = get_logger(__name__)

T = TypeVar('T', bound='CityData')

class CityData:
    """
    A facade for accessing and managing city data.
    
    This class coordinates the various repositories and managers to provide
    a unified interface for city data operations.
    """
    
    def __init__(self, db_uri: Optional[str] = None, persistent: bool = False) -> None:
        """
        Initialize the CityData manager.
        
        Args:
            db_uri: Database URI to connect to. If None, uses SQLite in the data directory.
            persistent: Whether to keep database connections open (for worker processes)
        """
        # If no URI provided, use SQLite in data directory
        if db_uri is None:
            data_dir = get_data_directory()
            db_uri = f"sqlite:///{os.path.join(data_dir, 'cities.db')}"
            
        worker_id = os.environ.get('GUNICORN_WORKER_ID', 'standalone')
        logger.info(f"Process {worker_id}: Initializing CityData with database URI: {db_uri} (persistent: {persistent})")
        
        # Initialize managers and repositories
        self.db_manager = DatabaseManager(db_uri, persistent=persistent)
        self.schema_manager = SchemaManager(self.db_manager)
        self.data_importer = CityDataImporter(self.db_manager)
        
        # Use the shared memory singleton repository pattern
        logger.info(f"Process {worker_id}: Initializing repositories")
        self.city_repository = get_city_repository(self.db_manager)
        self.geo_repository = get_geo_repository(self.db_manager)
        self.region_repository = get_region_repository(self.db_manager)
        
        # We should only check schema and import data in non-worker processes or standalone mode
        # Workers should assume master has already verified the database
        is_worker = worker_id != 'standalone'
        
        if not is_worker:
            # Ensure the schema exists
            self.schema_manager.ensure_schema_exists()
            
            # Check if database is empty and try to import data if needed
            try:
                count = self.get_table_info()['row_count']
                if count == 0:
                    logger.info("Database is empty. Attempting to import city data...")
                    self.import_city_data()
            except Exception as e:
                logger.warning(f"Error checking database content: {e}. Will try to import data if needed.")
                # Try to import data anyway
                try:
                    self.import_city_data()
                except Exception as import_err:
                    logger.error(f"Failed to import city data during initialization: {import_err}")
        
        # Keep track of persistence
        self.persistent = persistent
    
    def import_city_data(self, csv_path: Optional[str] = None, batch_size: int = 5000) -> bool:
        """
        Import city data from a CSV file.
        
        Args:
            csv_path: Path to the CSV file to import. If None, attempts to find a default file.
            batch_size: Number of records to import at once.
            
        Returns:
            True if the import was successful, False otherwise.
        """
        try:
            # First check if we already have data
            count = self.get_table_info().get('row_count', 0)
            if count > 0:
                logger.info(f"Database already contains {count} cities. Import not needed.")
                return True
                
            # Try to import from provided or found csv
            imported = self.data_importer.import_from_csv(csv_path, batch_size, download_if_missing=True)
            return imported > 0
        except Exception as e:
            logger.error(f"Error importing city data: {str(e)}")
            # If import failed and we don't have a specific path, try to download explicitly
            if csv_path is None:
                try:
                    from GeoDash.data.importer import download_city_data
                    csv_path = download_city_data(force=True)
                    logger.info(f"Retrying import with freshly downloaded data: {csv_path}")
                    
                    # Clear the database first
                    with self.db_manager.cursor() as cursor:
                        cursor.execute(f"DELETE FROM city_data")
                        logger.info("Cleared existing data before retrying import")
                    
                    imported = self.data_importer.import_from_csv(csv_path, batch_size, download_if_missing=False)
                    return imported > 0
                except Exception as download_err:
                    logger.error(f"Error during retry with explicit download: {str(download_err)}")
            return False
    
    @lru_cache(maxsize=5000)
    def search_cities(
        self, 
        query: str, 
        limit: int = 10, 
        country: Optional[str] = None, 
        user_lat: Optional[float] = None, 
        user_lng: Optional[float] = None, 
        user_country: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Search for cities by name with optional location-aware prioritization.
        
        Args:
            query: The search query (city name or prefix)
            limit: Maximum number of results to return
            country: Optional country filter (restricts results to this country)
            user_lat: User's latitude for location-aware prioritization
            user_lng: User's longitude for location-aware prioritization
            user_country: User's country for location-aware prioritization
            
        Returns:
            List of matching cities as dictionaries, prioritized by proximity
            to user's location when provided
        """
        return self.city_repository.search(
            query, 
            limit, 
            country, 
            user_lat=user_lat, 
            user_lng=user_lng, 
            user_country=user_country
        )
    
    @lru_cache(maxsize=1000)
    def get_city(self, city_id: int) -> Optional[Dict[str, Any]]:
        """
        Get a city by its ID.
        
        Args:
            city_id: The ID of the city to fetch
            
        Returns:
            City details as a dictionary or None if not found
        """
        return self.city_repository.get_by_id(city_id)
    
    def get_cities_by_coordinates(
        self, 
        lat: float, 
        lng: float, 
        radius_km: float = 10
    ) -> List[Dict[str, Any]]:
        """
        Find cities within a given radius from the specified coordinates.
        
        Args:
            lat: Latitude of the center point
            lng: Longitude of the center point
            radius_km: Search radius in kilometers (default: 10)
            
        Returns:
            List of cities within the radius, ordered by distance
        """
        return self.geo_repository.find_by_coordinates(lat, lng, radius_km)
    
    @lru_cache(maxsize=1)
    def get_countries(self) -> List[str]:
        """
        Get a list of all countries.
        
        Returns:
            List of country names, sorted alphabetically
        """
        return self.region_repository.get_countries()
    
    @lru_cache(maxsize=100)
    def get_states(self, country: str) -> List[str]:
        """
        Get a list of states in a country.
        
        Args:
            country: Country name
            
        Returns:
            List of state names, sorted alphabetically
        """
        return self.region_repository.get_states(country)
    
    @lru_cache(maxsize=500)
    def get_cities_in_state(self, state: str, country: str) -> List[Dict[str, Any]]:
        """
        Get a list of cities in a state.
        
        Args:
            state: State name
            country: Country name
            
        Returns:
            List of cities in the state, sorted by name
        """
        return self.region_repository.get_cities_in_state(state, country)
    
    def get_table_info(self) -> Dict[str, Any]:
        """
        Get information about the city_data table.
        
        Returns:
            Dictionary with table information including columns and row count
        """
        return self.schema_manager.get_table_info()
    
    def close(self) -> None:
        """
        Close the database connection.
        
        This should be called when the CityData instance is no longer needed
        to release database resources. For persistent connections used by
        worker processes, this is a no-op unless force=True.
        """
        if hasattr(self, 'db_manager') and self.db_manager:
            if not hasattr(self, 'persistent') or not self.persistent:
                self.db_manager.close()
                logger.debug("Closed non-persistent database connection")
            # Persistent connections are kept open until the process exits
    
    def __enter__(self: T) -> T:
        """
        Enter context manager.
        
        Returns:
            The CityData instance
        """
        return self
        
    def __exit__(self, exc_type: Optional[Type[BaseException]], 
                exc_value: Optional[BaseException], 
                traceback: Optional[Any]) -> None:
        """
        Exit context manager.
        
        Args:
            exc_type: Exception type if an exception was raised
            exc_value: Exception value if an exception was raised
            traceback: Traceback if an exception was raised
        """
        self.close() 