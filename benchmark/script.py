# Copyright 2015 ClusterHQ Inc.  See LICENSE file for details.
"""
Run the control service benchmarks.
"""

from datetime import datetime
from functools import partial
import json
import os
from platform import node, platform
import sys

from jsonschema import FormatChecker, Draft4Validator
import yaml

from eliot import to_file

from twisted.internet.task import react
from twisted.python.usage import Options, UsageError

from flocker import __version__ as flocker_client_version

from benchmark._driver import driver

to_file(sys.stderr)


class BenchmarkOptions(Options):
    description = "Run benchmark tests."

    optParameters = [
        ['control', None, None,
         'IP address for a Flocker cluster control server.'],
        ['certs', None, 'certs',
         'Directory containing client certificates'],
        ['config', None, 'benchmark.yml',
         'YAML file describing benchmark options.'],
        ['scenario', None, 'default',
         'Environmental scenario under which to perform test.'],
        ['operation', None, 'default', 'Operation to measure.'],
        ['metric', None, 'default', 'Quantity to benchmark.'],
    ]


def usage(options, message=None):
    sys.stderr.write(options.getUsage())
    sys.stderr.write('\n')
    sys.exit(message)


def validate_configuration(configuration):
    """
    Validate a provided configuration.

    :param dict configuration: A desired configuration.
    :raises: jsonschema.ValidationError if the configuration is invalid.
    """
    schema = {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "type": "object",
        "required": ["scenarios", "operations", "metrics"],
        "properties": {
            "scenarios": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["name", "type"],
                    "properties": {
                        "name": {
                            "type": "string"
                        },
                        "type": {
                            "type": "string"
                        },
                    },
                    "additionalProperties": "true",
                },
            },
            "operations": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["name", "type"],
                    "properties": {
                        "name": {
                            "type": "string"
                        },
                        "type": {
                            "type": "string"
                        },
                    },
                    "additionalProperties": "true",
                },
            },
            "metrics": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["name", "type"],
                    "properties": {
                        "name": {
                            "type": "string"
                        },
                        "type": {
                            "type": "string"
                        },
                    },
                    "additionalProperties": "true",
                },
            }
        }
    }

    v = Draft4Validator(schema, format_checker=FormatChecker())
    v.validate(configuration)


def main():
    options = BenchmarkOptions()

    try:
        options.parseOptions()
    except UsageError as e:
        usage(options, e.args[0])

    if not options['control'] and options['operation'] != 'no-op':
        # No-op is OK with no control service
        usage(options, 'Control service required')

    with open(options['config'], 'rt') as f:
        config = yaml.safe_load(f)
        validate_configuration(config)
        scenarios = config['scenarios']
        operations = config['operations']
        metrics = config['metrics']

    scenario_name = options['scenario']
    operation_name = options['operation']
    metric_name = options['metric']

    def get_config(section, name):
        for config in section:
            if config['name'] == name:
                return config
        return None

    scenario_config = get_config(scenarios, scenario_name) or usage(
        options, 'No such scenario: {!r}'.format(scenario_name))
    operation_config = get_config(operations, operation_name) or usage(
        options, 'No such operation: {!r}'.format(operation_name))
    metric_config = get_config(metrics, metric_name) or usage(
        options, 'No such metric: {!r}'.format(metric_name))

    timestamp = datetime.now().isoformat()

    result = dict(
        timestamp=timestamp,
        client=dict(
            flocker_version=flocker_client_version,
            working_directory=os.getcwd(),
            username=os.environ[b"USER"],
            nodename=node(),
            platform=platform(),
        ),
        scenario=scenario_config,
        operation=operation_config,
        metric=metric_config,
    )

    react(
        driver, (
            options, scenario_config.copy(), operation_config.copy(),
            metric_config.copy(), result,
            partial(json.dump, fp=sys.stdout, indent=2)
        )
    )

if __name__ == '__main__':
    main()
