# Copyright ClusterHQ Inc.  See LICENSE file for details.

"""
Helpers for Ubuntu cloud images.
"""

import json
import sys
from urllib2 import urlopen

from pyrsistent import PClass, field, pmap, freeze
from twisted.python.usage import Options, UsageError

URL_BASE = u"https://cloud-images.ubuntu.com/query"
URL_LATEST_TEMPLATE = URL_BASE + u"/{release_cycle}.latest.txt"
URL_DETAILS_TEMPLATE = (
    URL_BASE + u"/{ubuntu_name}/{ubuntu_variant}/{release_cycle}.current.txt"
)


def url_mangle(template, template_variables):
    template_variables = template_variables.copy()
    release_cycle = template_variables["release_cycle"]
    if release_cycle == u"release":
        release_cycle = u"released"
    template_variables["release_cycle"] = release_cycle
    return template.format(**template_variables)


def new_record_type(name, headings):
    return type(
        name,
        (PClass,),
        {heading: field(type=unicode, mandatory=True) for heading in headings}
    )


HEADINGS_BASE = (
    "ubuntu_name",
    "ubuntu_variant",
    "release_cycle",
    "release_date",
)

HEADINGS_DETAIL = HEADINGS_BASE + (
    "ec2_image_type",
    "architecture",
    "region",
    "ami_id",
    "aki_id",
    "unknown1",
    "hypervisor",
)

UbuntuBase = new_record_type("UbuntuBase", HEADINGS_BASE)
UbuntuDetail = new_record_type("UbuntuDetail", HEADINGS_DETAIL)

HEADINGS_BY_TYPE = pmap({
    UbuntuBase: HEADINGS_BASE,
    UbuntuDetail: HEADINGS_DETAIL,
})


def pclass_issubset(subset, superset):
    return set(
        subset.serialize().items()
    ).issubset(
        set(superset.serialize().items())
    )


def filter_for_pclass(pclass_type, pclass_kwargs):
    pclass_fields = pclass_type._pclass_fields
    # Does the pclass support all the proposed kwargs
    unexpected_keys = set(
        pclass_kwargs.keys()
    ).difference(
        set(pclass_fields.keys())
    )
    if unexpected_keys:
        raise ValueError(
            "Unexpected keyword arguments for '{}'. "
            "Found: {}. "
            "Expected: {}.".format(
                pclass_type.__name__,
                list(unexpected_keys),
                pclass_fields.keys(),
            )
        )

    fields_to_validate = {
        key: field
        for key, field
        in pclass_fields.items()
        if key in pclass_kwargs
    }
    filter_type = type(
        "Filter_{}".format(pclass_type.__name__),
        (PClass,),
        fields_to_validate
    )
    return filter_type(**pclass_kwargs)


def url_records(url, record_type):
    headings = HEADINGS_BY_TYPE[record_type]
    for line in urlopen(url).readlines():
        yield record_type(
            **dict(zip(
                headings,
                line.decode('utf-8').rstrip("\n").split("\t"))
            )
        )


def filter_records(records, search_record):
    for record in records:
        if pclass_issubset(
                subset=search_record,
                superset=record
        ):
            yield record


def latest(**kwargs):
    [latest] = filter_records(
        records=url_records(
            url_mangle(URL_LATEST_TEMPLATE, kwargs),
            UbuntuBase,
        ),
        search_record=filter_for_pclass(
            pclass_type=UbuntuBase,
            pclass_kwargs=dict(
                (k, kwargs[k])
                for k in ("ubuntu_name", "ubuntu_variant", "release_cycle")
            )
        )
    )
    return filter_records(
        records=url_records(
            url_mangle(URL_DETAILS_TEMPLATE, kwargs),
            UbuntuDetail,
        ),
        search_record=filter_for_pclass(
            pclass_type=UbuntuDetail,
            pclass_kwargs=freeze(kwargs).set(
                "release_date", latest.release_date,
            )
        )
    )


class AMISearchUbuntuOptions(Options):
    """
    Options.
    """
    optParameters = [
        ['release-cycle', None, u"daily",
         'One of `daily` or `release`.', unicode],
        ['ubuntu-name', None, u"trusty",
         'An Ubuntu release name.', unicode],
    ]


def ami_search_ubuntu_main(args, top_level, base_path):
    options = AMISearchUbuntuOptions()
    try:
        options.parseOptions(args)
    except UsageError as e:
        sys.stderr.write("%s: %s\n" % (base_path.basename(), e))
        raise SystemExit(1)

    print json.dumps(
        {
            r.region: r.ami_id
            for r in latest(
                release_cycle=options["release-cycle"],
                ubuntu_name=options["ubuntu-name"],
                ubuntu_variant=u"server",
                ec2_image_type=u'ebs',
                architecture=u'amd64',
                hypervisor=u'hvm',
            )
        },
        sort_keys=True
    )
