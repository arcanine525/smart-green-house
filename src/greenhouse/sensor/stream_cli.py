"""Local replay streaming entrypoint with explicit publication scope."""
import argparse
import os
import sys

from greenhouse.settings import Settings


def stream_command(argv=None):
    parser = argparse.ArgumentParser(prog='python -m greenhouse stream',
        description='Land collector files and publish a scoped replay snapshot locally.')
    for option, env in (('stream-id', 'GREENHOUSE_STREAM_ID'),
                        ('generation', 'GREENHOUSE_CHECKPOINT_GENERATION'),
                        ('replay-id', 'GREENHOUSE_REPLAY_ID'),
                        ('scope-id', 'GREENHOUSE_REPLAY_SCOPE_ID'),
                        ('cycle-id', 'GREENHOUSE_REPLAY_CYCLE_ID'),
                        ('compartment-id', 'GREENHOUSE_REPLAY_COMPARTMENT_ID'),
                        ('quality-policy-version', 'GREENHOUSE_REPLAY_QUALITY_POLICY')):
        default = os.environ.get(env)
        parser.add_argument('--' + option, default=default, required=not default)
    parser.add_argument('--backend',choices=('postgres','clickhouse'),default=os.environ.get('GREENHOUSE_BACKEND','postgres'))
    parser.add_argument('--coordinator',default=os.environ.get('GREENHOUSE_CH_COORDINATOR','/candidate/coordinator'))
    parser.add_argument('--replay-config-version',default=os.environ.get('GREENHOUSE_CH_REPLAY_CONFIG_VERSION'))
    parser.add_argument('--ready-dir', default='/data/ready')
    parser.add_argument('--checkpoint-dir', default='/data/checkpoints/replay')
    parser.add_argument('--available-now', action='store_true')
    parser.add_argument('--max-rows-per-batch', type=int, default=50000)
    parser.add_argument('--max-files-per-batch', type=int, default=20)
    args = parser.parse_args(argv)
    from greenhouse.sensor.stream_landing import start_landing
    options = {name: getattr(args, name) for name in
               ('replay_id', 'scope_id', 'cycle_id', 'compartment_id', 'quality_policy_version')}
    try:
        if args.backend=='clickhouse':
            if not args.replay_config_version:raise ValueError('ClickHouse replay configuration version is required')
            from pathlib import Path
            from greenhouse.clickhouse.client import ClickHouseSettings
            from greenhouse.clickhouse.stream_store import StreamSettings
            configuration=StreamSettings(ClickHouseSettings.from_env(),Path(args.coordinator),args.replay_config_version)
        else:
            configuration=Settings.from_env()
        query = start_landing(configuration, args.ready_dir, args.checkpoint_dir,
                              args.stream_id, args.generation, available_now=args.available_now,
                              max_rows_per_batch=args.max_rows_per_batch, max_files_per_batch=args.max_files_per_batch,
                              publication_options=options)
        query.awaitTermination()
    except KeyboardInterrupt:
        query.stop()
        return 130
    except Exception as error:
        # Exceptions can contain DSNs; report the type without leaking credentials.
        print(f'stream failed: {type(error).__name__}', file=sys.stderr)
        return 1
    return 0
