# /data/boltz_webui/lead_optimization/exceptions.py

"""
Custom exceptions for lead optimization module
"""

class OptimizationError(Exception):
    """Base exception for optimization-related errors"""
    pass

class MMPDatabaseError(OptimizationError):
    """Exceptions related to MMP database operations"""
    pass

class BoltzAPIError(OptimizationError):
    """Exceptions related to Boltz-WebUI API interactions"""
    pass

class InvalidCompoundError(OptimizationError):
    """Exceptions for invalid compound structures"""
    pass

class ScoringError(OptimizationError):
    """Exceptions related to scoring calculations"""
    pass

class ConfigurationError(OptimizationError):
    """Exceptions related to configuration issues"""
    pass
