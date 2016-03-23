# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import json
import re
import os

from itertools import (
    product as iter_product,
    ifilter,
)
from functools import partial

from collections import defaultdict, Sequence, MutableMapping
from contextlib import contextmanager

from configman import Namespace, RequiredConfig, class_converter
from configman.dotdict import DotDict as ConfigmanDotDict
from configman.converters import list_converter, to_str

from socorro.analysis.correlations import macdebugids
from socorro.analysis.correlations import addonids

from socorrolib.app.fetch_transform_save_app import (
    FetchTransformSaveWithSeparateNewCrashSourceApp
)

from socorrolib.lib.datetimeutil import UTC
from socorrolib.lib.transform_rules import Rule
from socorrolib.lib.util import DotDict as SocorroDotDict
from socorrolib.lib.converters import change_default
from socorro.external.crashstorage_base import CrashIDNotFound
from socorro.external.postgresql.products import ProductVersions
from socorro.processor.processor_2015 import rule_sets_from_string

#------------------------------------------------------------------------------
correlation_rule_sets = [
    [
        "correlation_rules",
        "correlation",
        "socorrolib.lib.transform_rules.TransformRuleSystem",
        "apply_all_rules",
        "socorro.analysis.correlations.core_count_rule"
            ".CorrelationCoreCountRule, "
        "socorro.analysis.correlations.interesting_rule"
            ".CorrelationInterestingModulesRule,"
        "socorro.analysis.correlations.interesting_rule"
            ".CorrelationInterestingModulesVersionsRule,"
        "socorro.analysis.correlations.interesting_rule"
            ".CorrelationInterestingAddonsRule,"
        "socorro.analysis.correlations.interesting_rule"
            ".CorrelationInterestingAddonsVersionsRule,"
    ],
]
correlation_rule_sets_as_string = json.dumps(correlation_rule_sets)


def date_with_default_today(value):
    if not value:
        return datetime.datetime.utcnow().date() - datetime.timedelta(days=1)
    y, m, d = [int(x) for x in value.split('-')]
    return datetime.date(y, m, d)


#==============================================================================
class CorrelationsApp(FetchTransformSaveWithSeparateNewCrashSourceApp):
    """"""
    app_name = 'correlations'
    app_version = '2.0'
    app_description = """the dbaron correlations scripts evolved"""

    required_config = Namespace()
    required_config.namespace('rules')
    required_config.rules.add_option(
        name='rule_sets',
        doc="a hierarchy of rules in json form",
        default=correlation_rule_sets_as_string,
        from_string_converter=rule_sets_from_string,
        likely_to_be_changed=True,
    )

    required_config.add_option(
        'transaction_executor_class',
        default="socorro.database.transaction_executor."
        "TransactionExecutorWithInfiniteBackoff",
        doc='a class that will manage transactions',
        from_string_converter=class_converter,
        reference_value_from='resource.postgresql',
    )
    required_config.add_option(
        'database_class',
        default=(
            'socorro.external.postgresql.connection_context'
            '.ConnectionContext'
        ),
        doc='the class responsible for connecting to Postgres',
        from_string_converter=class_converter,
        reference_value_from='resource.postgresql',
    )

    required_config.add_option(
        name='date',
        doc='Specific date to run this for',
        default='',
        from_string_converter=date_with_default_today,
    )

    required_config.add_option(
        name='product',
        doc='Product name',
        default='Firefox',
    )

    #--------------------------------------------------------------------------
    def __init__(self, config, quit_check_callback=None):
        super(CorrelationsApp, self).__init__(config)
        # self.database = config.database_class(
        #     config
        # )
        # self.transaction = config.transaction_executor_class(
        #     config,
        #     self.database,
        #     quit_check_callback=quit_check_callback
        # )

    #--------------------------------------------------------------------------
    @staticmethod
    def get_application_defaults():
        return {
            "number_of_submissions": 'all',
            "source.crashstorage_class":
               'socorro.external.boto.crashstorage.BotoS3CrashStorage',
            "destination.crashstorage_class":
                'socorro.external.crashstorage_base.NullCrashStorage',
            "new_crash_source.new_crash_source_class":
                'socorro.external.es.new_crash_source.ESNewCrashSource',
        }

    #--------------------------------------------------------------------------
    def _create_iter(self):
        hits = ProductVersions(config=self.config).get(
            active=True,
            product=self.config.product
        )['hits']
        versions = [
            x['version'] for x in hits if not x['version'].endswith('b')
        ]
        assert versions, "No active versions"

        # convert a datetime.date object to datetime.datetime
        dt = datetime.datetime(
            self.config.date.year,
            self.config.date.month,
            self.config.date.day,
        ).replace(tzinfo=UTC)
        return self.new_crash_source.new_crashes(
            dt,
            product=self.config.product,
            versions=versions,
        )


    #--------------------------------------------------------------------------
    def _transform(self, crash_id):
        """Take a raw_crash and its associated raw_dumps and return a
        processed_crash.
        """
        try:
            processed_crash = self.source.get_unredacted_processed(
                crash_id
            )
        except CrashIDNotFound:
            self.config.logger.warning('%s cannot be found - skipping')
            raise

        raw_crash = {}
        raw_dumps = {}
        meta_data = {}

        # apply transformations
        #    step through each of the rule sets to apply the rules.
        for a_rule_set_name, a_rule_set in self.rule_system.iteritems():
            # for each rule set, invoke the 'act' method - this method
            # will be the method specified in fourth element of the
            # rule set configuration list.
            a_rule_set.act(
                raw_crash,
                raw_dumps,
                processed_crash,
                meta_data
            )
            self.quit_check()

    #--------------------------------------------------------------------------
    def _setup_source_and_destination(self):
        super(CorrelationsApp, self)._setup_source_and_destination()
        self.rule_system = ConfigmanDotDict()
        for a_rule_set_name in self.config.rules.rule_sets.names:
            self.config.logger.debug(
                'setting up rule set: %s',
                a_rule_set_name
            )
            self.rule_system[a_rule_set_name] = (
                self.config.rules[a_rule_set_name].rule_system_class(
                    self.config.rules[a_rule_set_name],
                    self.quit_check
                )
            )

    #--------------------------------------------------------------------------
    def close(self):
        super(CorrelationsApp, self).close()
        self.config.logger.debug('CorrelationsApp closes')
        for a_rule_set_name, a_rule_set in self.rule_system.iteritems():
            self.config.logger.debug('closing %s', a_rule_set_name)
            a_rule_set.close()
        self.config.logger.debug('done closing rules')
