"""Resumable sample of complete matches; hours are a planning estimate only."""
import argparse
import importlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import DATA_DIR, REGIONS, ROUTING_REGIONS
from database import weekend_store as store
from utils.csv_export import write_csv_atomic
from utils.logger import get_logger
from utils.riot_api import RiotAPIError, get_match_ids
from utils.timeline import EVENT_FIELDNAMES, MILESTONE_FIELDNAMES

details = importlib.import_module('collector.3_collect_details')
timelines = importlib.import_module('collector.4_collect_timelines')
logger = get_logger('collector_weekend')


class RunLock:
    """Prevent simultaneous sample collectors; released by the OS after a crash."""
    def __enter__(self):
        self.file = open(Path(DATA_DIR) / 'weekend.lock', 'a+b')
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('Another weekend collector is already running')
        return self

    def __exit__(self, *args):
        self.file.close()


def export_sample(run):
    output = Path(DATA_DIR) / 'weekend'
    if run['name'] != 'weekend':
        output = output / run['name']
    for name, table, fields in (
        ('dataset.csv', 'match_summaries', details.FIELDNAMES),
        ('events.csv', 'events', EVENT_FIELDNAMES),
        ('milestones.csv', 'milestones', MILESTONE_FIELDNAMES),
    ):
        write_csv_atomic(output / name, fields, store.iter_complete_rows(run['name'], table))
    logger.info('Sample exported to %s: %s', output, store.progress(run['name']))


def discover(run):
    store.prepare_sources(run)
    for puuid, region in store.pending_sources(run['name']):
        try:
            ids = get_match_ids(ROUTING_REGIONS[region], puuid, count=100, raise_on_error=True)
            if any(not isinstance(mid, str) or '_' not in mid for mid in ids):
                raise RiotAPIError('Invalid match ID history')
            store.save_history(run['name'], puuid, ids, ROUTING_REGIONS[region])
        except RiotAPIError as error:
            store.source_error(run['name'], puuid, str(error))
            logger.warning('History pending in %s: %s', region, error)
            if error.status_code in (401, 403):
                raise


def worker(run):
    if not run['selection_done'] and not store.has_inputs(json.loads(run['regions_json'])):
        logger.error('No hay jugadores ni partidas en las regiones de esta muestra. '
                     'Ejecuta primero: .\\.venv\\Scripts\\python.exe collector/1_collect_players.py; '
                     'despues vuelve a ejecutar collector/collect_weekend.py.')
        return 1
    exit_code = 0
    started = time.monotonic()
    try:
        if not run['selection_done']:
            discover(run)
            quotas = store.select_matches(run)
            if not any(quotas.values()):
                logger.error('No match IDs available after discovery. Check API errors/history and rerun; '
                             'the selection remains pending.')
                return 1
            logger.info('Fixed sample selected by region: %s', quotas)
        for index, (match_id, routing, processed) in enumerate(store.pending_matches(run['name']), 1):
            if not processed and not details.process_match(match_id, routing):
                continue
            timelines.process_match(match_id, routing)
            if index % 100 == 0:
                logger.info('Progress %s; %.2fh elapsed in this session',
                            store.progress(run['name']), (time.monotonic() - started) / 3600)
            if index % 2000 == 0:
                export_sample(run)
    except KeyboardInterrupt:
        logger.info('Interrupted. The selected sample and completed matches are preserved.')
        exit_code = 130
    except RiotAPIError:
        logger.error('Authentication failed. Update .env and rerun to continue.')
        exit_code = 1
    finally:
        if store.progress(run['name'])['selected']:
            export_sample(run)
    return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hours', type=float, default=40,
                        help='Planning reference in hours; never stops collection')
    parser.add_argument('--max-matches', type=int, default=30000)
    parser.add_argument('--players-per-region', type=int, default=100)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--run-name', default='weekend')
    parser.add_argument('--plan', action='store_true', help='Inspect capacity without API calls or starting a run')
    parser.add_argument('--export-only', action='store_true', help='Recover CSV without downloading')
    args = parser.parse_args(argv)
    if not math.isfinite(args.hours) or args.hours <= 0 or args.max_matches <= 0:
        parser.error('--hours must be finite and positive; --max-matches must be positive')
    if not 0 <= args.players_per_region <= 200 or not REGIONS:
        parser.error('Configure at least one region; --players-per-region must be in [0, 200]')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args.run_name):
        parser.error('Invalid run name')
    if args.plan:
        from utils.rate_limiter import limiter
        per_request = max(limiter.long_window_seconds / limiter.long_window_limit,
                          limiter.short_window_seconds / limiter.short_window_limit)
        logger.info('Existing IDs by configured region: %s', store.availability(REGIONS))
        logger.info('Target <=%s matches, ideal two-request lower bound %.2fh; '
                    'planning reference %.2fh. There is no time cutoff; actual duration varies.',
                    args.max_matches, args.max_matches * 2 * per_request / 3600,
                    args.hours)
        return 0
    try:
        with RunLock():
            if args.export_only:
                run = store.load_run(args.run_name)
                if run is None:
                    logger.error('No existing run to export. Start collection first.')
                    return 1
                return export_recovery(run)
            run = store.open_run(args.run_name, args.hours, args.max_matches, args.seed,
                                 REGIONS, args.players_per_region)
            logger.info('Run %s: target %s matches; collection has no time cutoff.',
                        run['name'], run['target'])
            return worker(run)
    except RuntimeError as error:
        logger.error('%s', error)
        return 1


def export_recovery(run):
    export_sample(run)
    return 0


if __name__ == '__main__':
    sys.exit(main())
