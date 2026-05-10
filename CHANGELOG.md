# Changelog

All notable changes to Newspaparr will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-05-09

### Added
- One-time NYT session capture via embedded Chromium + noVNC, replacing per-renewal browser automation
- HTTP-only renewer (`renewer.py`) — full library auth → EZproxy → NYT redemption flow over `httpx`, ~1s per account
- Library-card password encryption at rest using Fernet with a key derived from `SECRET_KEY` (PBKDF2-HMAC-SHA256)
- Auto-generated `SECRET_KEY` persisted to `data/secret_key` (mode 0600) on first boot
- Apprise-based notifications — fires on renewal failure and on recovery from a previous failure (~80 services supported)
- Inline-SVG icon helper (`icons.py`) — drops the Font Awesome CDN dependency
- Built Tailwind bundle in `static/css/app.css` (no CDN at runtime); rebuild via `scripts/build-css.sh`
- Smoke tests for at-rest encryption and capture-session lifecycle

### Changed
- App now runs on Python 3.13
- `app.py` split into focused modules: `models.py`, `forms.py`, `scheduler.py`, `helpers.py`, `extensions.py`, `paths.py`
- `app.py` is now Flask routes only
- Activity log now records the full final-page URL for every attempt

### Removed
- Selenium, undetected-chromedriver, and the entire library-adapter / state-detector framework
- CapSolver integration and SOCKS5 proxy support — no CAPTCHA solving needed for the HTTP flow
- WSJ support (NYT-only going forward; library WSJ access has been spotty for years)
- Dead `error_handling.py` module
- Deprecated `libgconf-2-4` from the Docker image

### Fixed
- Capture-session subprocesses leaked across gunicorn worker reloads
- `DEBUG_MODE` / `FLASK_DEBUG` documentation mismatch in compose example
- Empty-password UX on the edit-account form (clarified that leaving blank preserves the existing password)
- `datetime.utcnow()` deprecation warnings under Python 3.13

## [1.0.0] - 2026-05-09

### Changed
- Foundational redesign: cookie-bridge architecture replaces Selenium-driven renewals
- This release was rolled directly into the 1.1.0 cut; see the 1.1.0 entry for the full set of changes that shipped publicly

## [0.6.0] - 2025-10-08

### Fixed
- Critical bug preventing automatic rescheduling after failed renewal attempts
- Added comprehensive error handling to ensure continuous renewal scheduling

## [0.5.7] - 2025-08-07

### Fixed
- Expiration date extraction now uses case-insensitive regex matching
- Fixed scheduling to properly use detected expiration dates
- Resolved issue where renewals defaulted to 24h intervals instead of using actual expiration

## [0.5.6] - 2025-08-07

### Changed
- Improved docker-compose.example.yml with clearer documentation
- Updated .gitignore for better configuration management

### Security
- Enhanced configuration file handling

## [0.5.5] - 2025-08-07

### Fixed
- Add 1 minute to all renewal intervals for reliability
- Update UI to show actual scheduled interval (e.g., "24h 1m")
- Fix interval-based fallback scheduling to include +1 minute
- Add form hint explaining the +1 minute behavior

## [0.5.4] - 2025-08-07

### Fixed
- Remove duplicate renewal start logging
- Ensure renewal block appears first in logs
- Reduce verbosity of initialization logs

## [0.5.3] - 2025-08-07

### Fixed
- Fix AttributeError: Account object has no 'library' attribute
- Display friendly library name instead of type code in logs

## [0.5.2] - 2025-08-07

### Fixed
- Timezone-aware scheduling: renewals now scheduled for exact expiration time + 1 minute
- Expiration date extraction properly handles user timezone
- All datetime storage and comparisons use UTC

### Added
- python-dateutil dependency for robust date parsing

## [0.5.1] - 2025-08-07

### Changed
- Switched from Google Chrome to Chromium for ARM64 support
- Improved logging format and reduced verbosity
- Fixed proxy port configuration bug

### Added
- Multi-architecture Docker support (AMD64 + ARM64)

## [0.5.0] - 2025-08-07

### 🎉 Initial Public Release

First public release of Newspaparr - an automated library card renewal system for digital newspaper access.

### ✨ Features

#### Core Functionality
- **Automated Daily Renewals** - Keep newspaper access active without manual intervention
- **Multi-Account Support** - Manage multiple library cards and newspaper accounts
- **Multiple Newspapers** - Support for The New York Times (NYT) and Wall Street Journal (WSJ)
- **OCLC Library Integration** - Works with OCLC-affiliated libraries offering digital passes

#### Advanced Automation
- **Priority-Based Login System** - Adaptive authentication handling for different newspaper flows
- **Smart State Detection** - Intelligent detection of success, warning, and failure states
- **CAPTCHA Solving** - Integrated CapSolver support with on-demand SOCKS5 proxy
- **Anti-Detection Measures** - Built with undetected-chromedriver and stealth techniques

#### User Interface
- **Modern Web Dashboard** - Clean, responsive interface built with Tailwind CSS
- **Real-Time Monitoring** - Live status updates and renewal tracking
- **Activity Logs** - Detailed history with filtering and search
- **Dark Mode Support** - Automatic theme switching

#### Technical Features
- **Docker Deployment** - Simple containerized setup
- **Persistent Storage** - SQLite database with automatic backups
- **Comprehensive Logging** - Rotating log files with configurable levels
- **Screenshot Debugging** - Automatic screenshots for troubleshooting
- **Health Monitoring** - Built-in health check endpoints

### 📋 Requirements
- Docker and Docker Compose
- Active library card from OCLC-affiliated library
- CapSolver account for CAPTCHA solving
- Port 1851 for web interface
- Port 3333 for SOCKS5 proxy

### 🏗️ Architecture
- Flask-based web application
- Selenium automation with undetected-chromedriver
- SQLite database for configuration and history
- APScheduler for automated renewals
- CapSolver integration for CAPTCHA challenges

### 📝 Notes
- Initial release focused on stability and core functionality
- Extensively tested with NYT and WSJ
- Production-ready with comprehensive error handling
- Full documentation included

---

## Pre-Release Development

### [0.4.0] - Internal Testing
- Implemented state detection system
- Added warning states for accounts with direct subscriptions
- Improved UI with color-coded status badges

### [0.3.0] - Beta Testing
- Added CAPTCHA solving via CapSolver
- Implemented SOCKS5 proxy for IP consistency
- Enhanced anti-detection measures

### [0.2.0] - Alpha Testing
- Basic renewal functionality
- Web interface implementation
- Database schema design

### [0.1.0] - Initial Development
- Project structure setup
- Core automation logic
- Library adapter framework

---

[0.5.0]: https://github.com/yourusername/newspaparr/releases/tag/v0.5.0