"""First reproducible mining experiment: draft prediction and objective rules.

Reads only dataset.csv and milestones.csv. No Riot requests or database writes.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import platform
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.analysis_csv import read_analysis_csv

import joblib
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import sklearn
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, balanced_accuracy_score, confusion_matrix,
                             log_loss, roc_auc_score, roc_curve)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder


LOGGER = logging.getLogger('mining')
CHAMPIONS = [f't{team}_{role}' for team in (1, 2)
             for role in ('top', 'jungle', 'middle', 'bottom', 'support')]
FEATURES = CHAMPIONS + ['patch_group']


def load_data(path, min_seconds=300):
    data = read_analysis_csv(path, dtype={'win_team1': 'string'})
    required = set(CHAMPIONS + ['match_id', 'game_creation', 'game_duration', 'patch', 'win_team1'])
    if required - set(data.columns):
        raise ValueError('Dataset is missing columns: ' + str(sorted(required - set(data.columns))))
    if data['match_id'].isna().any() or data['match_id'].duplicated().any():
        raise ValueError('Each match_id must be present and unique')
    data['target'] = data['win_team1'].map({'True': 1, 'False': 0})
    if data['target'].isna().any() or data[CHAMPIONS].isna().any().any():
        raise ValueError('Invalid winner labels or missing champion roles')
    data['target'] = data['target'].astype(int)
    original_count = len(data)
    data = data.loc[data['game_duration'] >= min_seconds].copy()
    data['patch_group'] = data['patch'].astype(str).str.extract(r'^(\d+\.\d+)', expand=False)
    data['started_at'] = pd.to_datetime(data['game_creation'], unit='ms', utc=True)
    data['ended_at'] = data['started_at'] + pd.to_timedelta(data['game_duration'], unit='s')
    if data[FEATURES + ['started_at', 'ended_at']].isna().any().any():
        raise ValueError('Invalid patch, timestamp or input feature')
    data = data.sort_values(['started_at', 'match_id']).reset_index(drop=True)
    return data, {'original_matches': original_count, 'removed_short_matches': original_count - len(data),
                  'usable_matches': len(data), 'minimum_duration_seconds': min_seconds}


def temporal_split(data):
    if len(data) < 100:
        raise ValueError('At least 100 usable matches are required for this experiment')
    validation_start = data.iloc[int(len(data) * .70)]['started_at']
    test_start = data.iloc[int(len(data) * .85)]['started_at']
    train = data.loc[(data['started_at'] < validation_start) & (data['ended_at'] < validation_start)]
    validation = data.loc[(data['started_at'] >= validation_start) & (data['started_at'] < test_start)
                          & (data['ended_at'] < test_start)]
    test = data.loc[data['started_at'] >= test_start]
    for name, part in (('train', train), ('validation', validation), ('test', test)):
        if len(part) == 0 or part['target'].nunique() != 2:
            raise ValueError(f'{name} needs both winner classes and at least one row')
    return train, validation, test


def make_model(c=1.0):
    return Pipeline([
        ('encode', ColumnTransformer([
            ('categories', OneHotEncoder(handle_unknown='ignore'), FEATURES),
        ], remainder='drop')),
        ('model', LogisticRegression(C=c, solver='lbfgs', max_iter=2000, random_state=42)),
    ])


def evaluate(y, probability):
    predicted = (probability >= .5).astype(int)
    return {'matches': len(y), 'accuracy': float(accuracy_score(y, predicted)),
            'balanced_accuracy': float(balanced_accuracy_score(y, predicted)),
            'roc_auc': float(roc_auc_score(y, probability)),
            'log_loss': float(log_loss(y, probability, labels=[0, 1])),
            'confusion_matrix': confusion_matrix(y, predicted, labels=[0, 1]).tolist()}


def objective_rules(data, path, minute=15, min_cases=30):
    """Descriptive association rules; unknown actor teams and late events excluded."""
    milestones = read_analysis_csv(path)
    if milestones['match_id'].duplicated().any():
        raise ValueError('Duplicated milestone match IDs')
    merged = data[['match_id', 'target']].merge(milestones, on='match_id', how='inner', validate='one_to_one')
    if len(merged) != len(data):
        raise ValueError('Some usable matches have no milestone row')
    results = []
    for objective in ('tower', 'dragon', 'herald', 'horde', 'baron', 'inhibitor'):
        for team_id, prefix in ((100, 't1'), (200, 't2')):
            team = pd.to_numeric(merged[f'first_{objective}_team'], errors='coerce')
            timestamp = pd.to_numeric(merged[f'first_{objective}_time'], errors='coerce')
            condition = (team == team_id) & timestamp.ge(0) & timestamp.le(minute * 60000)
            cases = int(condition.sum())
            if cases < min_cases:
                continue
            won = merged['target'].eq(1 if team_id == 100 else 0)
            joint = int((condition & won).sum())
            baseline = float(won.mean())
            confidence = joint / cases
            results.append({'antecedent': f'{prefix}_first_{objective}_by_{minute}min',
                            'consequent': f'{prefix}_wins', 'cases': cases, 'joint_wins': joint,
                            'antecedent_support': cases / len(merged), 'support': joint / len(merged),
                            'confidence': confidence, 'baseline_win_rate': baseline,
                            'lift': confidence / baseline})
    columns = ['antecedent', 'consequent', 'cases', 'joint_wins', 'antecedent_support',
               'support', 'confidence', 'baseline_win_rate', 'lift']
    return pd.DataFrame(results, columns=columns).sort_values(['confidence', 'cases'], ascending=False)


def save_figures(output, y_test, probabilities, dummy_probability, coefficients, learning):
    plt.rcParams.update({'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False})
    fig, ax = plt.subplots(figsize=(6, 5))
    for label, probability in (('Draft: regresion logistica', probabilities), ('Referencia', dummy_probability)):
        fpr, tpr, _ = roc_curve(y_test, probability)
        ax.plot(fpr, tpr, label=f'{label} (AUC={roc_auc_score(y_test, probability):.3f})')
    ax.plot([0, 1], [0, 1], '--', color='gray', linewidth=1)
    ax.set(xlabel='Tasa de falsos positivos', ylabel='Tasa de verdaderos positivos', title='ROC: partidas futuras de prueba')
    ax.legend(loc='lower right')
    fig.tight_layout()
    fig.savefig(output / 'roc_curve.png', dpi=180)
    plt.close(fig)
    matrix = confusion_matrix(y_test, probabilities >= .5, labels=[0, 1])
    fig, ax = plt.subplots(figsize=(5, 4))
    image = ax.imshow(matrix, cmap='Blues')
    for i in range(2):
        for j in range(2):
            ax.text(j, i, str(matrix[i, j]), ha='center', va='center')
    ax.set(xticks=[0, 1], yticks=[0, 1], xticklabels=['Equipo 2', 'Equipo 1'],
           yticklabels=['Equipo 2', 'Equipo 1'], xlabel='Predicho', ylabel='Real', title='Matriz de confusion: prueba')
    fig.colorbar(image, ax=ax)
    fig.tight_layout()
    fig.savefig(output / 'confusion_matrix.png', dpi=180)
    plt.close(fig)
    top = coefficients.nlargest(20, 'absolute_weight').sort_values('coefficient')
    fig, ax = plt.subplots(figsize=(10, 7))
    ax.barh(top['feature'].str.replace('categories__', '', regex=False), top['coefficient'],
            color=['#d36b55' if value < 0 else '#397a9b' for value in top['coefficient']])
    ax.axvline(0, color='gray', linewidth=1)
    ax.set(xlabel='Coeficiente (asociacion, no causalidad)', title='Variables con mayor peso absoluto')
    fig.tight_layout()
    fig.savefig(output / 'feature_weights.png', dpi=180)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot(learning['training_matches'], learning['validation_auc'], marker='o')
    ax.axhline(.5, linestyle='--', color='gray')
    ax.set(xlabel='Partidas de entrenamiento', ylabel='ROC AUC en validacion fija',
           title='Curva exploratoria de aprendizaje')
    fig.tight_layout()
    fig.savefig(output / 'learning_curve.png', dpi=180)
    plt.close(fig)


def run(dataset, milestones, output, min_seconds=300, minute=15):
    output.mkdir(parents=True, exist_ok=True)
    data, cleaning = load_data(dataset, min_seconds)
    train, validation, test = temporal_split(data)
    LOGGER.info('Usable matches: %s; train=%s, validation=%s, test=%s',
                len(data), len(train), len(validation), len(test))
    tuning = []
    for c in (.01, .1, 1., 10.):
        candidate = make_model(c)
        candidate.fit(train[FEATURES], train['target'])
        probability = candidate.predict_proba(validation[FEATURES])[:, 1]
        score = evaluate(validation['target'], probability)
        tuning.append({'C': c, 'validation_auc': score['roc_auc'], 'validation_log_loss': score['log_loss']})
        LOGGER.info('C=%s, validation AUC=%.4f', c, score['roc_auc'])
    best_c = max(tuning, key=lambda row: (row['validation_auc'], -row['validation_log_loss']))['C']
    # Validation selects C but does not fit the encoder, prior or coefficients.
    final = make_model(best_c).fit(train[FEATURES], train['target'])
    dummy = DummyClassifier(strategy='prior').fit(train[FEATURES], train['target'])
    probability = final.predict_proba(test[FEATURES])[:, 1]
    dummy_probability = dummy.predict_proba(test[FEATURES])[:, 1]
    scores = {'draft_logistic': evaluate(test['target'], probability),
              'dummy_prior': evaluate(test['target'], dummy_probability)}
    feature_names = final.named_steps['encode'].get_feature_names_out()
    weights = final.named_steps['model'].coef_[0]
    occurrences = np.asarray(final.named_steps['encode'].transform(train[FEATURES]).sum(axis=0)).ravel()
    coefficients = pd.DataFrame({'feature': feature_names, 'coefficient': weights,
                                 'absolute_weight': np.abs(weights), 'training_occurrences': occurrences.astype(int)})
    coefficients.sort_values('absolute_weight', ascending=False).to_csv(output / 'feature_weights.csv', index=False)
    rng = np.random.default_rng(42)
    order = rng.permutation(len(train))
    learning = []
    for size in sorted({min(n, len(train)) for n in (5000, 10000, 15000, len(train))}):
        subset = train.iloc[order[:size]]
        model = make_model(best_c).fit(subset[FEATURES], subset['target'])
        learning.append({'training_matches': size, 'validation_auc': float(roc_auc_score(
            validation['target'], model.predict_proba(validation[FEATURES])[:, 1]))})
    learning = pd.DataFrame(learning)
    learning.to_csv(output / 'learning_curve.csv', index=False)
    rules = objective_rules(data, milestones, minute)
    rules.to_csv(output / 'objective_rules.csv', index=False)
    pd.DataFrame(tuning).to_csv(output / 'validation_results.csv', index=False)
    predictions = test[['match_id', 'started_at', 'patch', 'target']].copy()
    predictions['probability_team1'] = probability
    predictions['predicted_team1'] = (probability >= .5).astype(int)
    predictions.to_csv(output / 'test_predictions.csv', index=False)
    partitions = {name: {'matches': len(part), 'first_start_utc': part['started_at'].min().isoformat(),
                          'last_start_utc': part['started_at'].max().isoformat(),
                          'team1_win_rate': float(part['target'].mean())}
                  for name, part in (('train', train), ('validation', validation), ('test', test))}
    split_ids = pd.concat([part[['match_id']].assign(split=name)
                           for name, part in (('train', train), ('validation', validation), ('test', test))])
    split_ids.to_csv(output / 'split_membership.csv', index=False)
    report = {'generated_at_utc': datetime.now(timezone.utc).isoformat(),
              'dataset_sha256': hashlib.sha256(dataset.read_bytes()).hexdigest(),
              'cleaning': cleaning, 'split': partitions,
              'purged_boundary_matches': len(data) - sum(len(part) for part in (train, validation, test)),
              'features': FEATURES, 'excluded_features': ['current average tiers', 'final objectives', 'duration', 'timestamps', 'match ID'],
              'best_C': best_c, 'scores_on_untouched_test': scores,
              'fit_policy': 'All learned transforms and final estimators fit on train only; validation selects C.',
              'versions': {'python': platform.python_version(), 'pandas': pd.__version__,
                           'numpy': np.__version__, 'scikit-learn': sklearn.__version__},
              'limitations': ['Observational associations do not establish causation.',
                              'Current team tiers are excluded because historical rank is unavailable.',
                              'Champion interactions and player skill are not modeled.',
                              'Players may occur in multiple matches; this is not a player-disjoint evaluation.',
                              'Learning curve is exploratory, uses one fixed validation period and one random seed.']}
    (output / 'metrics.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    joblib.dump(final, output / 'draft_model.joblib')
    save_figures(output, test['target'], probability, dummy_probability, coefficients, learning)
    lines = ['# Primer experimento de mineria', '',
             f"Partidas originales: {cleaning['original_matches']:,}. Partidas de al menos cinco minutos: {len(data):,}.",
             '', '## Clasificacion: ganador desde la seleccion de campeones', '',
             'Entradas: diez campeones por rol y parche. Salida: victoria del equipo 1.',
             'Se excluyen objetivos finales, duracion y rango actual para evitar informacion posterior al momento de prediccion.',
             'Separacion temporal aproximada 70/15/15; se eliminan partidas de entrenamiento/validacion que terminan despues del inicio del siguiente bloque.',
             'El encoder y los estimadores se ajustan solo con entrenamiento. C se elige en validacion y la prueba se evalua una vez sin reajustar con validacion.',
             '', '| Modelo | Accuracy | Balanced accuracy | ROC AUC | Log loss |', '|---|---:|---:|---:|---:|']
    for label, score in scores.items():
        lines.append(f"| {label} | {score['accuracy']:.4f} | {score['balanced_accuracy']:.4f} | {score['roc_auc']:.4f} | {score['log_loss']:.4f} |")
    if scores['draft_logistic']['accuracy'] <= scores['dummy_prior']['accuracy']:
        lines += ['', '**Resultado:** este modelo no supera a la referencia en accuracy. '
                  'Una AUC ligeramente mayor que 0.5 no basta para considerarlo un predictor util.']
    if scores['draft_logistic']['log_loss'] >= scores['dummy_prior']['log_loss']:
        lines += ['', 'El log loss tampoco mejora la referencia: sus probabilidades no son mejores en esta prueba.']
    lines += ['', 'El modelo Dummy predice con la frecuencia de clases del entrenamiento; es la referencia a superar.',
              'ROC AUC=0.5 representa una ordenacion al azar; valores superiores indican mayor capacidad de discriminar.',
              '', '![ROC](roc_curve.png)', '', '![Matriz de confusion](confusion_matrix.png)', '',
              '## Relacion entre objetivos tempranos y victoria', '',
              f'Reglas descriptivas: primera captura antes del minuto {minute} y victoria. Se omiten tiempos sin equipo conocido y reglas con menos de 30 casos.',
              'No se usan las banderas de primera sangre que discrepan entre fuentes. La confianza es la proporcion de victorias entre quienes cumplen la condicion; lift la compara con la tasa base del equipo.',
              'Estas asociaciones no prueban que capturar un objetivo cause la victoria; tambien pueden reflejar una ventaja previa.', '',
              '| Condicion | Casos | Confianza | Lift |', '|---|---:|---:|---:|']
    for row in rules.head(8).to_dict('records'):
        lines.append(f"| {row['antecedent']} => {row['consequent']} | {row['cases']:,} | {row['confidence']:.2%} | {row['lift']:.3f} |")
    lines += ['', '## Aprendizaje con mas partidas', '', '![Curva de aprendizaje](learning_curve.png)',
              'La curva usa subconjuntos aleatorios anidados del mismo bloque de entrenamiento y una validacion futura fija. Es una exploracion, no una garantia de que mas datos mejoren el resultado.',
              '', '## Variables y limites', '', '![Pesos](feature_weights.png)',
              'Los pesos son asociaciones del modelo y no una lista causal de campeones fuertes. Consultar tambien el numero de apariciones de cada variable.',
              'metrics.json incluye fechas, parametros, version de dependencias y limitaciones. split_membership.csv identifica los grupos para reproducir la evaluacion.',
              'Para predecir durante una partida hace falta un experimento separado que use exclusivamente eventos anteriores al minuto elegido.', '']
    (output / 'mining_report.md').write_text('\n'.join(lines), encoding='utf-8')
    LOGGER.info('Test results: %s', scores)
    LOGGER.info('Report saved: %s', output / 'mining_report.md')
    return report


def main():
    base = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=base / 'data' / 'weekend' / 'dataset.csv')
    parser.add_argument('--milestones', type=Path, default=base / 'data' / 'weekend' / 'milestones.csv')
    parser.add_argument('--output', type=Path, default=base / 'reports' / 'mining')
    parser.add_argument('--min-duration', type=int, default=300)
    parser.add_argument('--minute', type=int, default=15)
    args = parser.parse_args()
    if args.min_duration < 0 or args.minute <= 0:
        parser.error('Duration must be nonnegative and minute must be positive')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    run(args.dataset, args.milestones, args.output, args.min_duration, args.minute)


if __name__ == '__main__':
    main()
