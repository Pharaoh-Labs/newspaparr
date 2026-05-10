"""
Standardized error handling and logging utilities for Newspaparr
"""
import logging
import traceback
import functools
from typing import Optional, Callable, Any, Dict
from contextlib import contextmanager
from datetime import datetime


class StandardizedLogger:
    """Standardized logging wrapper with consistent formatting"""
    
    def __init__(self, name: str):
        import os
        import logging.handlers
        from paths import LOGS_DIR

        self.logger = logging.getLogger(name)
        # Ensure we have handlers configured
        if not self.logger.handlers and not logging.getLogger().handlers:
            # Configure basic logging if no handlers exist
            logs_dir = LOGS_DIR
            os.makedirs(logs_dir, exist_ok=True)
            
            # Add file handler
            file_handler = logging.handlers.RotatingFileHandler(
                os.path.join(logs_dir, 'newspaparr.log'),
                maxBytes=10*1024*1024,  # 10MB
                backupCount=5
            )
            file_handler.setFormatter(
                logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            )
            
            # Add to root logger so all loggers inherit it
            root_logger = logging.getLogger()
            root_logger.addHandler(file_handler)
            root_logger.setLevel(logging.INFO)
        
    def info(self, message: str, **kwargs):
        """Log info message with context"""
        self.logger.info(self._format_message(message, **kwargs))
    
    def warning(self, message: str, **kwargs):
        """Log warning message with context"""
        self.logger.warning(self._format_message(message, **kwargs))
    
    def error(self, message: str, error: Exception = None, **kwargs):
        """Log error message with optional exception details"""
        formatted_msg = self._format_message(message, **kwargs)
        if error:
            formatted_msg += f" | Error: {str(error)}"
        self.logger.error(formatted_msg)
        
        # Log stack trace for debugging if error provided
        if error and self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"Stack trace: {traceback.format_exc()}")
    
    def debug(self, message: str, **kwargs):
        """Log debug message with context"""
        self.logger.debug(self._format_message(message, **kwargs))
    
    def _format_message(self, message: str, **kwargs) -> str:
        """Format message with context"""
        if kwargs:
            context = " | ".join([f"{k}: {v}" for k, v in kwargs.items()])
            return f"{message} | {context}"
        return message


class ErrorContext:
    """Context manager for standardized error handling"""
    
    def __init__(self, 
                 operation: str,
                 logger: StandardizedLogger,
                 account_name: str = None,
                 newspaper_type: str = None,
                 raise_on_error: bool = False):
        self.operation = operation
        self.logger = logger
        self.account_name = account_name
        self.newspaper_type = newspaper_type
        self.raise_on_error = raise_on_error
        self.start_time = None
    
    def __enter__(self):
        self.start_time = datetime.now()
        self.logger.info(f"Starting {self.operation}", 
                        account=self.account_name, 
                        newspaper=self.newspaper_type)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        duration = (datetime.now() - self.start_time).total_seconds()
        
        if exc_type is None:
            self.logger.info(f"Completed {self.operation}", 
                           duration=f"{duration:.1f}s",
                           account=self.account_name,
                           newspaper=self.newspaper_type)
        else:
            self.logger.error(f"Failed {self.operation}", 
                            error=exc_val,
                            duration=f"{duration:.1f}s",
                            account=self.account_name,
                            newspaper=self.newspaper_type)
            
            if not self.raise_on_error:
                return True  # Suppress exception
        
        return False


def with_error_handling(operation: str, 
                       logger: StandardizedLogger = None,
                       default_return: Any = None,
                       raise_on_error: bool = False):
    """Decorator for standardized error handling"""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Use provided logger or create one based on function module
            log = logger or StandardizedLogger(func.__module__)
            
            try:
                log.debug(f"Starting {operation}", function=func.__name__)
                result = func(*args, **kwargs)
                log.debug(f"Completed {operation}", function=func.__name__)
                return result
                
            except Exception as e:
                log.error(f"Failed {operation}", error=e, function=func.__name__)
                
                if raise_on_error:
                    raise
                return default_return
                
        return wrapper
    return decorator


@contextmanager
def safe_execution(operation: str, 
                  logger: StandardizedLogger,
                  account_name: str = None,
                  default_return: Any = None):
    """Context manager for safe execution with standardized error handling"""
    try:
        logger.debug(f"Starting {operation}", account=account_name)
        yield
        logger.debug(f"Completed {operation}", account=account_name)
        
    except Exception as e:
        logger.error(f"Failed {operation}", error=e, account=account_name)
        return default_return


class RenewalErrorHandler:
    """Specialized error handler for renewal operations"""
    
    def __init__(self, account_name: str, newspaper_type: str):
        self.account_name = account_name
        self.newspaper_type = newspaper_type
        self.logger = StandardizedLogger(f"renewal.{newspaper_type}")
        
    def handle_library_auth_error(self, error: Exception) -> str:
        """Handle library authentication errors"""
        error_msg = str(error).lower()
        
        if "invalid" in error_msg or "incorrect" in error_msg:
            message = "❌ Library login failed - Invalid username or password"
        elif "timeout" in error_msg:
            message = "❌ Library login failed - Network timeout"
        elif "captcha" in error_msg:
            message = "❌ Library login failed - CAPTCHA challenge"
        else:
            message = f"❌ Library login failed - {str(error)[:100]}..."
            
        self.logger.error(f"Library authentication failed for {self.account_name}", 
                         error=error, 
                         newspaper=self.newspaper_type)
        return message
    
    def handle_newspaper_access_error(self, error: Exception) -> str:
        """Handle newspaper access errors"""
        error_msg = str(error).lower()
        
        if "not available" in error_msg or "unavailable" in error_msg:
            message = "❌ Access denied - Service not available from this library"
        elif "expired" in error_msg:
            message = "❌ Access denied - Library subscription expired"
        elif "geographic" in error_msg or "region" in error_msg:
            message = "❌ Access denied - Geographic restriction"
        else:
            message = f"❌ Access denied - {str(error)[:100]}..."
            
        self.logger.error(f"{self.newspaper_type} access failed for {self.account_name}", 
                         error=error)
        return message
    
    def handle_login_error(self, error: Exception) -> str:
        """Handle newspaper login errors"""
        error_msg = str(error).lower()
        
        if "invalid" in error_msg or "incorrect" in error_msg:
            message = "❌ Login failed - Email or password incorrect"
        elif "timeout" in error_msg:
            message = "❌ Login failed - Network timeout"
        elif "element" in error_msg or "selector" in error_msg:
            message = "❌ Login failed - Required elements missing"
        elif "page" in error_msg and "load" in error_msg:
            message = "❌ Login failed - Page not loading properly"
        else:
            message = f"❌ Login failed - {str(error)[:100]}..."
            
        self.logger.error(f"{self.newspaper_type} login failed for {self.account_name}", 
                         error=error)
        return message
    
    def handle_captcha_error(self, error: Exception, solved: bool = False) -> str:
        """Handle CAPTCHA-related errors"""
        if solved:
            message = "❌ Blocked by CAPTCHA - Automatic solving failed"
        else:
            message = "❌ Blocked by CAPTCHA - Solver unavailable"
            
        self.logger.error(f"CAPTCHA error for {self.account_name}", 
                         error=error, 
                         newspaper=self.newspaper_type,
                         solved=solved)
        return message


def setup_logging(debug: bool = False):
    """Setup standardized logging configuration"""
    level = logging.DEBUG if debug else logging.INFO
    
    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Setup root logger
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Reduce noise from external libraries
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('PIL').setLevel(logging.WARNING)


# Convenience functions for backward compatibility
def get_logger(name: str) -> StandardizedLogger:
    """Get a standardized logger"""
    return StandardizedLogger(name)


def log_renewal_operation(account_name: str, 
                         newspaper_type: str, 
                         operation: str,
                         logger: StandardizedLogger = None):
    """Context manager for logging renewal operations"""
    log = logger or StandardizedLogger('renewal')
    return ErrorContext(operation, log, account_name, newspaper_type)