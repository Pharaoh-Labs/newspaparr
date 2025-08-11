"""
Text-Based State Detection for Newspaper Renewals
Based on real-world screenshot analysis of success/failure states
"""

from typing import Tuple, Optional
from selenium.webdriver.common.by import By
from error_handling import StandardizedLogger

logger = StandardizedLogger(__name__)


class StateDetector:
    """Detects renewal states based on empirical patterns from actual renewals"""
    
    # Track CAPTCHA attempts per context
    captcha_attempts = {}
    
    # Track library portal visits (for stuck detection)
    library_portal_count = 0
    
    @staticmethod
    def check_state(driver, newspaper_type: str, context: str = "") -> Tuple[str, Optional[str]]:
        """
        Check current page state using empirical patterns
        
        Returns:
            Tuple of (state, message) where state is one of:
            - SUCCESS: Definitive success, stop processing
            - SUCCESS_WITH_WARNING: Success but with a caveat
            - FAILURE: Definitive failure, stop processing  
            - CAPTCHA_PRESENT: CAPTCHA detected, needs solving
            - CONTINUE: No definitive state, continue processing
        """
        try:
            # Get page text
            page_text = driver.find_element(By.TAG_NAME, 'body').text.lower()
            current_url = driver.current_url.lower()
            
            # Log key patterns found for debugging (only in specific contexts)
            if context == "final_verification":
                StateDetector._log_detected_patterns(page_text, newspaper_type)
            
            # Check SUCCESS states first (most specific patterns)
            success_result = StateDetector._check_success_patterns(page_text, newspaper_type)
            if success_result[0] != "CONTINUE":
                return success_result
            
            # Check FAILURE states (conservative - only definitive failures)
            failure_result = StateDetector._check_failure_patterns(page_text, current_url, newspaper_type)
            if failure_result[0] == "FAILURE":
                return failure_result
            
            # Check for CAPTCHA presence
            captcha_result = StateDetector._check_captcha_presence(driver, context)
            if captcha_result[0] == "CAPTCHA_PRESENT":
                return captcha_result
            
            # No definitive state found
            return ("CONTINUE", None)
            
        except Exception as e:
            logger.error(f"Error in state detection: {str(e)}")
            return ("CONTINUE", None)
    
    @staticmethod
    def _check_success_patterns(page_text: str, newspaper_type: str) -> Tuple[str, Optional[str]]:
        """Check for success patterns based on empirical data"""
        
        if newspaper_type == 'nyt':
            # NYT SUCCESS patterns (very specific to avoid false positives)
            if "your pass is active and will expire on" in page_text:
                return ("SUCCESS", "NYT pass active with expiration")
            
            if "you've claimed your nytimes pass!" in page_text:
                return ("SUCCESS", "NYT pass claimed successfully")
            
            # NYT SUCCESS_WITH_WARNING patterns
            # Check for "already associated" which means account has direct subscription
            if "already associated with an active new york times subscription" in page_text:
                return ("SUCCESS_WITH_WARNING", "NYT account has direct subscription (no library pass needed)")
        
        elif newspaper_type == 'wsj':
            # WSJ SUCCESS patterns
            if "welcome back" in page_text and "looks like you already have a subscription" in page_text:
                return ("SUCCESS", "WSJ pass active from previous claim")
            
            # WSJ Redemption successful pattern
            if "redemption successful" in page_text:
                logger.info("✅ WSJ redemption successful pattern detected")
                return ("SUCCESS", "WSJ redemption successful")
            
            # WSJ subscription confirmation patterns
            if "thank you for subscribing" in page_text and "you now have full access to wsj.com" in page_text:
                logger.info("✅ WSJ subscription confirmation detected")
                return ("SUCCESS", "WSJ subscription confirmed - Full access granted")
            
            # WSJ confirmation number pattern (indicates successful redemption)
            if "confirmation no." in page_text or "confirmation number" in page_text:
                logger.info("✅ WSJ confirmation number detected - indicates successful redemption")
                return ("SUCCESS", "WSJ redemption confirmed with confirmation number")
        
        return ("CONTINUE", None)
    
    @staticmethod
    def _check_failure_patterns(page_text: str, current_url: str, newspaper_type: str) -> Tuple[str, Optional[str]]:
        """Check for failure patterns - conservative to avoid false positives"""
        
        # Authentication errors (very specific)
        auth_errors = [
            "incorrect password",
            "invalid email", 
            "wrong username",
            "invalid username",
            "incorrect email",
            "wrong password",
            "authentication failed",
            "login failed"
        ]
        
        for error in auth_errors:
            if error in page_text:
                return ("FAILURE", f"Authentication error: {error}")
        
        # Access denied/blocked
        if "access denied" in page_text:
            return ("FAILURE", "Access denied")
        
        if "account locked" in page_text:
            return ("FAILURE", "Account locked")
        
        if "blocked" in page_text and "captcha" not in page_text:  # Avoid CAPTCHA "blocked" messages
            return ("FAILURE", "Access blocked")
        
        # Check if stuck at library portal (WSJ specific pattern)
        if newspaper_type == 'wsj':
            if "public library" in page_text and "visit the wall street journal" in page_text:
                StateDetector.library_portal_count += 1
                
                if StateDetector.library_portal_count >= 3:
                    return ("FAILURE", "Stuck at library portal after 3 attempts")
                else:
                    logger.info(f"At library portal (attempt {StateDetector.library_portal_count}/3)")
        else:
            # Reset counter if not at library portal
            StateDetector.library_portal_count = 0
        
        return ("CONTINUE", None)
    
    @staticmethod
    def _check_captcha_presence(driver, context: str) -> Tuple[str, Optional[str]]:
        """Check for CAPTCHA presence - not a failure unless we can't solve it"""
        
        # CAPTCHA indicators
        captcha_selectors = [
            "iframe[src*='captcha']",
            "iframe[title*='CAPTCHA']",
            "iframe[title*='DataDome']",
            "[class*='captcha']",
            "[id*='captcha']"
        ]
        
        captcha_present = False
        for selector in captcha_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    captcha_present = True
                    logger.debug(f"CAPTCHA detected via selector: {selector}")
                    break
            except:
                continue
        
        if captcha_present:
            # Track attempts per context
            if context not in StateDetector.captcha_attempts:
                StateDetector.captcha_attempts[context] = 0
            
            StateDetector.captcha_attempts[context] += 1
            
            # Only fail after multiple attempts at same location
            if StateDetector.captcha_attempts[context] > 3:
                return ("FAILURE", f"CAPTCHA blocking progress after 3 attempts at {context}")
            
            return ("CAPTCHA_PRESENT", f"CAPTCHA detected (attempt {StateDetector.captcha_attempts[context]}/3)")
        
        # Reset counter if no CAPTCHA
        if context in StateDetector.captcha_attempts:
            StateDetector.captcha_attempts[context] = 0
        
        return ("CONTINUE", None)
    
    @staticmethod
    def reset_captcha_counter(context: str):
        """Reset CAPTCHA counter for a specific context (e.g., after successful solve)"""
        if context in StateDetector.captcha_attempts:
            StateDetector.captcha_attempts[context] = 0
            logger.debug(f"Reset CAPTCHA counter for context: {context}")
    
    @staticmethod
    def reset_all_counters():
        """Reset all tracking counters (useful between renewal attempts)"""
        StateDetector.captcha_attempts = {}
        StateDetector.library_portal_count = 0
        logger.debug("Reset all state detection counters")
    
    @staticmethod
    def _log_detected_patterns(page_text: str, newspaper_type: str):
        """Log detected patterns for debugging purposes"""
        # Key patterns to look for
        patterns_to_check = {
            "redemption": "redemption" in page_text,
            "successful": "successful" in page_text,
            "confirmation": "confirmation" in page_text,
            "thank you": "thank you" in page_text,
            "full access": "full access" in page_text,
            "subscription": "subscription" in page_text,
            "welcome": "welcome" in page_text,
            "error": "error" in page_text,
            "failed": "failed" in page_text,
            "invalid": "invalid" in page_text,
            "expired": "expired" in page_text,
            "receipt": "receipt" in page_text,
            "email": "email" in page_text
        }
        
        detected = [key for key, found in patterns_to_check.items() if found]
        if detected:
            logger.info(f"📋 Patterns detected on {newspaper_type.upper()} page: {', '.join(detected)}")
        
        # Log specific phrases if found
        if "redemption successful" in page_text:
            logger.info("🎯 Found 'redemption successful' - marking as SUCCESS")
        if "confirmation no" in page_text or "confirmation number" in page_text:
            logger.info("🎯 Found confirmation number - marking as SUCCESS")
        if "thank you for subscribing" in page_text:
            logger.info("🎯 Found subscription thank you message - marking as SUCCESS")
        if "you now have full access" in page_text:
            logger.info("🎯 Found full access confirmation - marking as SUCCESS")


def check_current_state(driver, newspaper_type: str, context: str = "") -> Tuple[str, Optional[str]]:
    """
    Convenience function to check current state
    
    Returns:
        Tuple of (state, message) where state is one of:
        - SUCCESS: Stop and mark as successful
        - SUCCESS_WITH_WARNING: Success but with caveat
        - FAILURE: Stop and mark as failed
        - CAPTCHA_PRESENT: CAPTCHA needs solving
        - CONTINUE: Continue with priority flow
    """
    return StateDetector.check_state(driver, newspaper_type, context)