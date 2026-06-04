import logging
import configparser

def setup_logger(filename):
    config = configparser.ConfigParser()
    config.read('config.ini')
    level_str = config.get('DEFAULT', 'loglevel', fallback='ERROR').upper()
    numeric_level = getattr(logging, level_str, logging.ERROR)
    
    logging.basicConfig(
        filename=filename,
        level=numeric_level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        force=True
    )
    return logging.getLogger(__name__)
