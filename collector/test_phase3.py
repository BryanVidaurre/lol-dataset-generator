import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.logger import get_logger
from utils.logger import get_logger

logger = get_logger("test_collector_3")

def main():
    logger.info("Running TEST for Step 3: 100 matches only")
    
    test_csv = "data/dataset_test.csv"
    
    # Remove existing test CSV to start fresh
    if os.path.exists(test_csv):
        os.remove(test_csv)
        
    # We call the process_batch from 3_collect_details with limit 100 and point to test_csv
    # It will extract 100 matches that have processed=0, and it will set them to processed=1.
    # Since these are real matches from the DB, it's a real test.
    
    # We use importlib to bypass invalid module name (starts with number)
    import importlib
    collect_details = importlib.import_module("collector.3_collect_details")
    
    count = collect_details.process_batch(limit=100, output_csv=test_csv)
    
    logger.info(f"Test finished! Successfully extracted {count} matches to {test_csv}.")
    logger.info("Please inspect the CSV file to verify the column formatting.")

if __name__ == "__main__":
    main()
