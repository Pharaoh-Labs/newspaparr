"""
Standardized renewal status system for Newspaparr.
Provides consistent 3-state model: SUCCESS, SUCCESS_WITH_WARNING, FAILURE
"""
from enum import Enum
from typing import Optional, Tuple
import re
from datetime import datetime
from date_extractor import DateExtractor


class RenewalStatus(Enum):
    """Standardized renewal statuses"""
    SUCCESS = "success"
    SUCCESS_WITH_WARNING = "success_warning"
    FAILURE = "failure"


class RenewalMessage:
    """Standardized renewal message formatting"""
    
    # Success messages
    SUCCESS_VERIFIED = "✅ Renewal complete - Access verified"
    SUCCESS_WITH_EXPIRY = "✅ Renewal complete - Access verified (expires {})"
    
    # Warning messages
    WARN_DIRECT_SUBSCRIPTION = "⚠️ Renewal skipped - Account has direct subscription (no library pass needed)"
    WARN_UNCERTAIN_VERIFICATION = "⚠️ Renewal uncertain - Login successful but couldn't verify"
    WARN_PROCESS_UNCLEAR = "⚠️ Renewal may need attention - Process completed but status unclear"
    
    # Failure messages by category
    FAIL_LIBRARY_INVALID_CREDS = "❌ Library login failed - Invalid username or password"
    FAIL_LIBRARY_MAINTENANCE = "❌ Library login failed - System under maintenance"
    FAIL_LIBRARY_LOCKED = "❌ Library login failed - Account may be locked"
    
    FAIL_ACCESS_EXPIRED = "❌ Access denied - Library subscription expired"
    FAIL_ACCESS_UNAVAILABLE = "❌ Access denied - Service not available from this library"
    FAIL_ACCESS_GEO_RESTRICTED = "❌ Access denied - Geographic restriction"
    
    FAIL_TECH_PAGE_LOAD = "❌ Login failed - Page not loading properly"
    FAIL_TECH_ELEMENTS_MISSING = "❌ Login failed - Required elements missing"
    FAIL_TECH_TIMEOUT = "❌ Login failed - Network timeout"
    
    FAIL_CREDS_INVALID = "❌ Login failed - Email or password incorrect"
    FAIL_CREDS_GENERIC = "❌ Login failed - Invalid credentials"
    
    FAIL_CAPTCHA_SOLVE_FAILED = "❌ Blocked by CAPTCHA - Automatic solving failed"
    FAIL_CAPTCHA_NO_SOLVER = "❌ Blocked by CAPTCHA - Solver unavailable"
    
    @staticmethod
    def format_success(expiration_date: Optional[str] = None) -> str:
        """Format success message with optional expiration date"""
        if expiration_date:
            return RenewalMessage.SUCCESS_WITH_EXPIRY.format(expiration_date)
        return RenewalMessage.SUCCESS_VERIFIED


class StateDetector:
    """Detect renewal state from page content and context"""
    
    # Success patterns - using word boundaries to avoid false positives
    SUCCESS_PATTERNS = [
        r"\byour pass is active\b",  # Word boundaries prevent matching "btn--active"
        r"\blibrary pass activated\b",
        r"\bsuccessfully activated\b",
        r"\brenewal successful\b",
        r"\bsubscription.*(?:current|existing)\b",
        r"\baccess.*verified\b",
        r"\bredemption successful\b",  # WSJ redemption pattern
        r"\bthank you for subscribing\b",  # WSJ subscription confirmation
        r"\byou now have full access\b",  # WSJ full access confirmation
        r"\bconfirmation (?:no|number)\b"  # WSJ confirmation number pattern
    ]
    
    # Direct subscription patterns (warning state)
    DIRECT_SUB_PATTERNS = [
        "already associated with an active.*subscription",
        "already have.*subscription",
        "existing.*subscription.*active"
    ]
    
    # Failure patterns by category
    INVALID_CREDS_PATTERNS = [
        "invalid.*(?:username|password|credentials)",
        "incorrect.*(?:username|password|credentials)",
        "email address.*doesn't match",
        "password is incorrect"
    ]
    
    MAINTENANCE_PATTERNS = [
        "maintenance",
        "under maintenance",
        "temporarily unavailable"
    ]
    
    ACCESS_DENIED_PATTERNS = [
        "not available",
        "unavailable",
        "service.*not.*available",
        "subscription.*expired",
        "geographic.*restriction",
        "region.*not.*supported"
    ]
    
    @staticmethod
    def detect_state(page_text: str, 
                    url: str = "",
                    has_error: bool = False,
                    captcha_detected: bool = False,
                    captcha_solved: bool = False,
                    process_completed: bool = True) -> Tuple[RenewalStatus, str]:
        """
        Detect renewal state from page content and context.
        Returns (status, message) tuple.
        """
        text_lower = page_text.lower()
        
        # Check for direct subscription (warning state)
        if any(re.search(pattern, text_lower) for pattern in StateDetector.DIRECT_SUB_PATTERNS):
            return RenewalStatus.SUCCESS_WITH_WARNING, RenewalMessage.WARN_DIRECT_SUBSCRIPTION
        
        # Check for success patterns
        for pattern in StateDetector.SUCCESS_PATTERNS:
            if re.search(pattern, text_lower):
                # Log specific success pattern detected
                if "redemption successful" in text_lower:
                    print(f"✅ WSJ Redemption Successful detected - marking as SUCCESS")
                elif "confirmation" in text_lower and ("no" in text_lower or "number" in text_lower):
                    print(f"✅ WSJ Confirmation number detected - marking as SUCCESS")
                elif "thank you for subscribing" in text_lower:
                    print(f"✅ WSJ subscription thank you detected - marking as SUCCESS")
                
                # Extract expiration date if present
                expiration_date = StateDetector._extract_expiration_date(page_text)
                return RenewalStatus.SUCCESS, RenewalMessage.format_success(expiration_date)
        
        # WSJ specific: Check URL for success
        if "wsj.com" in url and not any(x in url for x in ["login", "activate", "partner.wsj.com"]):
            return RenewalStatus.SUCCESS, RenewalMessage.SUCCESS_VERIFIED
        
        # Check for CAPTCHA issues
        if captcha_detected:
            if not captcha_solved:
                if has_error:
                    return RenewalStatus.FAILURE, RenewalMessage.FAIL_CAPTCHA_SOLVE_FAILED
                else:
                    return RenewalStatus.FAILURE, RenewalMessage.FAIL_CAPTCHA_NO_SOLVER
            elif process_completed and not any(re.search(p, text_lower) for p in StateDetector.SUCCESS_PATTERNS):
                return RenewalStatus.SUCCESS_WITH_WARNING, RenewalMessage.WARN_PROCESS_UNCLEAR
        
        # Check for failure patterns
        if any(re.search(pattern, text_lower) for pattern in StateDetector.INVALID_CREDS_PATTERNS):
            return RenewalStatus.FAILURE, RenewalMessage.FAIL_CREDS_INVALID
        
        if any(re.search(pattern, text_lower) for pattern in StateDetector.MAINTENANCE_PATTERNS):
            return RenewalStatus.FAILURE, RenewalMessage.FAIL_LIBRARY_MAINTENANCE
        
        if any(re.search(pattern, text_lower) for pattern in StateDetector.ACCESS_DENIED_PATTERNS):
            if "expired" in text_lower:
                return RenewalStatus.FAILURE, RenewalMessage.FAIL_ACCESS_EXPIRED
            elif "geographic" in text_lower or "region" in text_lower:
                return RenewalStatus.FAILURE, RenewalMessage.FAIL_ACCESS_GEO_RESTRICTED
            else:
                return RenewalStatus.FAILURE, RenewalMessage.FAIL_ACCESS_UNAVAILABLE
        
        # Check for technical failures
        if has_error:
            if not page_text.strip():
                return RenewalStatus.FAILURE, RenewalMessage.FAIL_TECH_PAGE_LOAD
            else:
                return RenewalStatus.FAILURE, RenewalMessage.FAIL_TECH_ELEMENTS_MISSING
        
        # If process completed but we can't verify success
        if process_completed:
            return RenewalStatus.SUCCESS_WITH_WARNING, RenewalMessage.WARN_UNCERTAIN_VERIFICATION
        
        # Default failure
        return RenewalStatus.FAILURE, RenewalMessage.FAIL_TECH_ELEMENTS_MISSING
    
    @staticmethod
    def _extract_expiration_date(text: str) -> Optional[str]:
        """Extract expiration date from text if present"""
        # Use unified extractor to get both datetime and display string
        _, display_date = DateExtractor.extract_expiration(text, "StateDetector")
        return display_date


def determine_renewal_state(page_text: str,
                          url: str = "",
                          has_error: bool = False,
                          captcha_detected: bool = False,
                          captcha_solved: bool = False,
                          process_completed: bool = True,
                          **kwargs) -> Tuple[bool, str]:
    """
    Main function to determine renewal state.
    Returns (success: bool, message: str) for database logging.
    
    The success boolean indicates if the renewal should be considered successful
    for scheduling purposes (includes warnings as successful).
    """
    status, message = StateDetector.detect_state(
        page_text=page_text,
        url=url,
        has_error=has_error,
        captcha_detected=captcha_detected,
        captcha_solved=captcha_solved,
        process_completed=process_completed
    )
    
    # SUCCESS and SUCCESS_WITH_WARNING both count as success for DB
    success = status in (RenewalStatus.SUCCESS, RenewalStatus.SUCCESS_WITH_WARNING)
    
    return success, message