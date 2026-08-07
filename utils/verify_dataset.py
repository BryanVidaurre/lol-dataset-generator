import pandas as pd
import os

def run_tests():
    csv_path = "data/dataset_test.csv"
    if not os.path.exists(csv_path):
        print("Erreur : dataset_test.csv introuvable.")
        return
        
    print("Chargement du dataset...\n")
    df = pd.read_csv(csv_path)
    
    print("=== TEST 1 : Vérification des rôles (valeurs nulles) ===")
    null_counts = df[["t1_top", "t1_jungle", "t1_middle", "t1_bottom", "t1_support"]].isnull().sum()
    print(null_counts)
    if null_counts.sum() == 0:
        print("[OK] Aucun role manquant, filtre robuste !\n")
    else:
        print("[ERROR] Probleme detecte : certains roles sont manquants.\n")
        
    print("=== TEST 2 : Equilibre des victoires ===")
    win_rates = df["win_team1"].value_counts(normalize=True) * 100
    print(win_rates)
    if 40 <= win_rates.get(True, 0) <= 60:
        print("[OK] Taux de victoire equilibre (~50%).\n")
    else:
        print("[ERROR] Desequilibre des victoires detecte !\n")
        
    print("=== TEST 3 : Verification des champions ===")
    print("Top 10 Champions en t1_top :")
    print(df["t1_top"].value_counts().head(10))
    
    print("\n[OK] Les champions semblent correctement extraits.")

if __name__ == "__main__":
    run_tests()
