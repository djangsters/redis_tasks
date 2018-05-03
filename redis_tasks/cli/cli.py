# -*- coding: utf-8 -*-
"""
RQ command line tool
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

from functools import update_wrapper
import os
import sys

import click
from redis.exceptions import ConnectionError

from redis_tasks import Connection, __version__ as version
from redis_tasks.cli.helpers import (read_config_file, refresh,
                            setup_loghandlers_from_args,
                            show_both, show_queues, show_workers, CliConfig)
from redis_tasks.contrib.legacy import cleanup_ghosts
from redis_tasks.defaults import (DEFAULT_CONNECTION_CLASS, DEFAULT_JOB_CLASS,
                         DEFAULT_QUEUE_CLASS, DEFAULT_WORKER_CLASS)
from redis_tasks.utils import import_attribute


# Disable the warning that Click displays (as of Click version 5.0) when users
# use unicode_literals in Python 2.
# See http://click.pocoo.org/dev/python3/#unicode-literals for more details.
click.disable_unicode_literals_warning = True


shared_options = [
    click.option('--url', '-u',
                 envvar='RQ_REDIS_URL',
                 help='URL describing Redis connection details.'),
    click.option('--config', '-c',
                 envvar='RQ_CONFIG',
                 help='Module containing RQ settings.'),
    click.option('--worker-class', '-w',
                 envvar='RQ_WORKER_CLASS',
                 default=DEFAULT_WORKER_CLASS,
                 help='RQ Worker class to use'),
    click.option('--task-class', '-j',
                 envvar='RQ_JOB_CLASS',
                 default=DEFAULT_JOB_CLASS,
                 help='RQ Task class to use'),
    click.option('--queue-class',
                 envvar='RQ_QUEUE_CLASS',
                 default=DEFAULT_QUEUE_CLASS,
                 help='RQ Queue class to use'),
    click.option('--connection-class',
                 envvar='RQ_CONNECTION_CLASS',
                 default=DEFAULT_CONNECTION_CLASS,
                 help='Redis client class to use'),
]


def pass_cli_config(func):
    # add all the shared options to the command
    for option in shared_options:
        func = option(func)

    # pass the cli config object into the command
    def wrapper(*args, **kwargs):
        ctx = click.get_current_context()
        cli_config = CliConfig(**kwargs)
        return ctx.invoke(func, cli_config, *args[1:], **kwargs)

    return update_wrapper(wrapper, func)


@click.group()
@click.version_option(version)
def main():
    """RQ command line tool."""
    pass


@main.command()
@click.option('--all', '-a', is_flag=True, help='Empty all queues')
@click.argument('queues', nargs=-1)
@pass_cli_config
def empty(cli_config, all, queues, **options):
    """Empty given queues."""

    if all:
        queues = cli_config.queue_class.all(connection=cli_config.connection,
                                            task_class=cli_config.task_class)
    else:
        queues = [cli_config.queue_class(queue,
                                         connection=cli_config.connection,
                                         task_class=cli_config.task_class)
                  for queue in queues]

    if not queues:
        click.echo('Nothing to do')
        sys.exit(0)

    for queue in queues:
        num_tasks = queue.empty()
        click.echo('{0} tasks removed from {1} queue'.format(num_tasks, queue.name))


@main.command()
@click.option('--path', '-P', default='.', help='Specify the import path.')
@click.option('--interval', '-i', type=float, help='Updates stats every N seconds (default: don\'t poll)')
@click.option('--raw', '-r', is_flag=True, help='Print only the raw numbers, no bar charts')
@click.option('--only-queues', '-Q', is_flag=True, help='Show only queue info')
@click.option('--only-workers', '-W', is_flag=True, help='Show only worker info')
@click.option('--by-queue', '-R', is_flag=True, help='Shows workers by queue')
@click.argument('queues', nargs=-1)
@pass_cli_config
def info(cli_config, path, interval, raw, only_queues, only_workers, by_queue, queues,
         **options):
    """RQ command-line monitor."""

    if path:
        sys.path = path.split(':') + sys.path

    if only_queues:
        func = show_queues
    elif only_workers:
        func = show_workers
    else:
        func = show_both

    try:
        with Connection(cli_config.connection):
            refresh(interval, func, queues, raw, by_queue,
                    cli_config.queue_class, cli_config.worker_class)
    except ConnectionError as e:
        click.echo(e)
        sys.exit(1)
    except KeyboardInterrupt:
        click.echo()
        sys.exit(0)


@main.command()
@click.option('--burst', '-b', is_flag=True, help='Run in burst mode (quit after all work is done)')
@click.option('--name', '-n', help='Specify a different name')
@click.option('--path', '-P', default='.', help='Specify the import path.')
@click.option('--worker-ttl', type=int, help='Default worker timeout to be used')
@click.option('--verbose', '-v', is_flag=True, help='Show more output')
@click.option('--quiet', '-q', is_flag=True, help='Show less output')
@click.option('--sentry-dsn', envvar='SENTRY_DSN', help='Report exceptions to this Sentry DSN')
@click.option('--exception-handler', help='Exception handler(s) to use', multiple=True)
@click.option('--pid', help='Write the process ID number to a file at the specified path')
@click.argument('queues', nargs=-1)
@pass_cli_config
def worker(cli_config, burst, name, path, results_ttl,
           worker_ttl, verbose, quiet, sentry_dsn, exception_handler,
           pid, queues, **options):
    """Starts an RQ worker."""

    if path:
        sys.path = path.split(':') + sys.path

    settings = read_config_file(cli_config.config) if cli_config.config else {}
    # Worker specific default arguments
    queues = queues or settings.get('QUEUES', ['default'])
    sentry_dsn = sentry_dsn or settings.get('SENTRY_DSN')

    if pid:
        with open(os.path.expanduser(pid), "w") as fp:
            fp.write(str(os.getpid()))

    setup_loghandlers_from_args(verbose, quiet)

    try:

        cleanup_ghosts(cli_config.connection)
        exception_handlers = []
        for h in exception_handler:
            exception_handlers.append(import_attribute(h))

        queues = [cli_config.queue_class(queue,
                                         connection=cli_config.connection,
                                         task_class=cli_config.task_class)
                  for queue in queues]
        worker = cli_config.worker_class(queues,
                                         name=name,
                                         connection=cli_config.connection,
                                         default_worker_ttl=worker_ttl,
                                         task_class=cli_config.task_class,
                                         queue_class=cli_config.queue_class,
                                         exception_handlers=exception_handlers or None)

        # Should we configure Sentry?
        if sentry_dsn:
            from raven import Client
            from raven.transport.http import HTTPTransport
            from redis_tasks.contrib.sentry import register_sentry
            client = Client(sentry_dsn, transport=HTTPTransport)
            register_sentry(client, worker)

        worker.work(burst=burst)
    except ConnectionError as e:
        print(e)
        sys.exit(1)