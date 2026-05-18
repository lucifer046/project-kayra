# ===========================================================================================================
#                                   utils.py (Shared Helper Utilities)
# ===========================================================================================================
# This module implements core, reusable utility tools shared across the OPTIMUS application.
# Includes standardized logging frameworks and central path resolution primitives.
# ===========================================================================================================

import os
import logging


def get_project_root():
    """
    Returns the absolute path to the project root directory.
    Guarantees stable resource lookup across different module subfolders.
    """
    # Moves up one level from 'modules/' directory to project root
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def setup_logger(name, log_filename="optimus.log", level=logging.INFO):
    """
    Configures and returns a robust logger instance writing structured logs to the 'logs/' folder.
    
    Parameters:
        name (str): Unique name of the module generating logs.
        log_filename (str): Target filename inside the 'logs/' directory.
        level (logging level): Minimum threshold level for logged events.
    """
    root = get_project_root()
    logs_dir = os.path.join(root, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    
    log_path = os.path.join(logs_dir, log_filename)
    
    # Structured format: Timestamp - Module - Level - Message
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    handler = logging.FileHandler(log_path, encoding='utf-8')
    handler.setFormatter(formatter)
    
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # Avoid duplicate handler registration on multiple setups
    if not logger.handlers:
        logger.addHandler(handler)
        
    return logger
