import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tqdm import tqdm
from config.settings import ROUTING_REGIONS
from utils.riot_api import get_match_ids
from utils.logger import get_logger
from database import db_manager
import sqlite3
from config.settings import DB_PATH

logger = get_logger("test_collector_2")

def main():
    logger.info("Running TEST for Step 2: 10 players only")
    
    unprocessed_players = db_manager.get_unprocessed_players(limit=10)
    
    if not unprocessed_players:
        logger.info("No players found.")
        return
        
    for puuid, region in tqdm(unprocessed_players, desc="Testing 10 Players"):
        routing_region = ROUTING_REGIONS.get(region)
        
        start_index = 0
        matches_count_for_player = 0
        
        while True:
            matches = get_match_ids(
                routing_region=routing_region, 
                puuid=puuid, 
                start=start_index, 
                count=100
            )
            
            if not matches:
                break
                
            for match_id in matches:
                db_manager.save_match(match_id, routing_region)
                matches_count_for_player += 1
            
            if len(matches) < 100:
                break
                
            start_index += 100
        
        db_manager.mark_player_matches_collected(puuid)
        logger.info(f"Collected {matches_count_for_player} matches for player in {region}")

    # Check stats
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM matches")
        total_matches = cursor.fetchone()[0]
        logger.info(f"Total unique match IDs in DB now: {total_matches}")

if __name__ == "__main__":
    main()
