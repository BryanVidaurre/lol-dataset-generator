import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.db_manager import get_connection

def main():
    print("=== Statistiques de la Base de Données (Players) ===")
    
    with get_connection() as conn:
        cursor = conn.cursor()
        
        # 1. Total players
        cursor.execute("SELECT COUNT(*) FROM players")
        total = cursor.fetchone()[0]
        print(f"\nTotal des joueurs uniques récupérés : {total}")
        
        if total == 0:
            print("Aucun joueur n'a encore été récupéré.")
            return

        # 2. Distribution by region
        print("\n--- Répartition par Région ---")
        cursor.execute("SELECT region, COUNT(*) FROM players GROUP BY region ORDER BY COUNT(*) DESC")
        for row in cursor.fetchall():
            print(f"{row[0]}: {row[1]} joueurs")
            
        # 3. Distribution by tier
        print("\n--- Répartition par Tier ---")
        cursor.execute("SELECT tier, COUNT(*) FROM players GROUP BY tier ORDER BY COUNT(*) DESC")
        for row in cursor.fetchall():
            print(f"{row[0]}: {row[1]} joueurs")

if __name__ == "__main__":
    main()
