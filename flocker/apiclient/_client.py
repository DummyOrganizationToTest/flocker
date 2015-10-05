# Copyright ClusterHQ Inc.  See LICENSE file for details.

"""
Client for the Flocker REST API.
"""

from uuid import UUID, uuid4
from json import dumps
from datetime import datetime

from pytz import UTC

from ipaddr import IPNetwork, IPAddress, IPv4Address, IPv6Address

from zope.interface import Interface, implementer

from pyrsistent import PClass, field, pmap_field, pmap, pset

from eliot import ActionType, Field
from eliot.twisted import DeferredContext

from twisted.internet.defer import succeed, fail
from twisted.python.filepath import FilePath
from twisted.web.http import CREATED, OK, CONFLICT

from treq import json_content, content

from ..ca import treq_with_authentication
from ..control import Leases as LeasesModel, LeaseError, DockerImage


_LOG_HTTP_REQUEST = ActionType(
    "flocker:apiclient:http_request",
    [Field.forTypes("url", [bytes, unicode], "Request URL."),
     Field.forTypes("method", [bytes, unicode], "Request method."),
     Field("request_body", lambda o: o, "Request JSON body.")],
    [Field.forTypes("response_code", [int], "Response code."),
     Field("response_body", lambda o: o, "JSON response body.")],
    "A HTTP request.")


NoneType = type(None)


class Node(PClass):
    uuid = field(type=UUID, mandatory=True)
    public_address = field(
        type=(IPv4Address, IPv6Address),
        mandatory=True,
    )


class Container(PClass):
    """
    A container in the configuration.
    """
    node_uuid = field()
    name = field()
    image = field()


class ContainerState(PClass):
    node_uuid = field()
    name = field()
    image = field()


class Dataset(PClass):
    """
    A dataset in the configuration.

    :attr UUID primary: The node where the dataset should manifest.
    :attr int|None maximum_size: Size of the dataset in bytes or ``None``
        if no particular size was requested.
    :attr UUID dataset_id: The UUID of the dataset.
    :attr metadata: A mapping between unicode keys and values.
    """
    dataset_id = field(type=UUID, mandatory=True)
    primary = field(type=UUID, mandatory=True)
    maximum_size = field(type=(int, NoneType), mandatory=True)
    metadata = pmap_field(unicode, unicode)


class DatasetState(PClass):
    """
    The state of a dataset in the cluster.

    :attr primary: The ``UUID`` of the node where the dataset is manifest,
        or ``None`` if it has no primary manifestation.
    :attr int|None maximum_size: Maximum size of the dataset in bytes, or
        ``None`` if the maximum size is not set.
    :attr UUID dataset_id: The UUID of the dataset.
    :attr FilePath|None path: Filesytem path where the dataset is mounted,
        or ``None`` if not mounted.
    """
    dataset_id = field(type=UUID, mandatory=True)
    primary = field(type=(UUID, NoneType), mandatory=True)
    maximum_size = field(type=(int, NoneType), mandatory=True)
    path = field(type=(FilePath, NoneType), mandatory=True)


class Lease(PClass):
    """
    A lease on a dataset.

    :attr UUID dataset_id: The dataset for which the lease applies.
    :attr UUID node_uuid: The node for which the lease applies.
    :attr None|float|int expires: Time in seconds until lease expires, or
        ``None`` if it will not expire.
    """
    dataset_id = field(type=UUID, mandatory=True)
    node_uuid = field(type=UUID, mandatory=True)
    expires = field(type=(float, int, NoneType), mandatory=True)


class DatasetAlreadyExists(Exception):
    """
    The suggested dataset ID already exists.
    """


class LeaseAlreadyHeld(Exception):
    """
    A lease exists for the specified dataset ID on a different node.
    """


class ContainerAlreadyExists(Exception):
    """
    The specified container name is in use by another container.
    """


class IFlockerAPIV1Client(Interface):
    """
    The Flocker REST API v1 client.
    """
    def version():
        """
        Get version information from the API server.
        """

    def list_nodes():
        """
        Get information about active cluster nodes.
        """

    def create_container(primary, name, image):
        pass

    def list_containers_state():
        pass

    def create_dataset(primary, maximum_size=None, dataset_id=None,
                       metadata=pmap()):
        """
        Create a new dataset in the configuration.

        :param UUID primary: The node where the dataset should manifest.
        :param maximum_size: Size of new dataset in bytes (as ``int``) or
            ``None`` if no particular size is required (not recommended).
        :param dataset_id: If given, the UUID to use for the dataset.
        :param metadata: A mapping between unicode keys and values, to be
            stored as dataset metadata.

        :return: ``Deferred`` that fires after the configuration has been
            updated with resulting ``Dataset``, or errbacking with
            ``DatasetAlreadyExists``.
        """

    def move_dataset(primary, dataset_id):
        """
        Move the dataset to a new location.

        :param UUID primary: The node where the dataset should manifest.
        :param dataset_id: Which dataset to move.

        :return: ``Deferred`` that fires after the configuration has been
            updated with the resulting ``Dataset``.
        """

    def delete_dataset(dataset_id):
        """
        Delete a dataset.

        :param dataset_id: The UUID of the dataset to be deleted.

        :return: ``Deferred`` that fires with the ``Dataset`` that has just
        been deleted, after the configuration has been updated.
        """

    def list_datasets_configuration():
        """
        Return the configured datasets, excluding any datasets that
        have been deleted.

        :return: ``Deferred`` firing with iterable of ``Dataset``.
        """

    def list_datasets_state():
        """
        Return the actual datasets in the cluster.

        :return: ``Deferred`` firing with iterable of ``DatasetState``.
        """

    def acquire_lease(dataset_id, node_uuid, expires):
        """
        Acquire a lease on a dataset on a given node.

        If the lease already exists for the given dataset and node then
        this will renew the lease.

        :param UUID dataset_id: The dataset for which the lease applies.
        :param UUID node_uuid: The node for which the lease applies.
        :param None|float expires: Time in seconds until lease expires, or
            ``None`` if it will not expire.

        :return: ``Deferred`` firing with a ``Lease`` or failing with
            ``LeaseAlreadyHeld`` if the lease for this dataset is held for a
            different node.
        """

    def release_lease(dataset_id):
        """
        Release a lease.

        :param UUID dataset_id: The dataset for which the lease applies.

        :return: ``Deferred`` firing with the released ``Lease`` on success.
        """

    def list_leases():
        """
        Return current leases.

        :return: ``Deferred`` firing with a list of ``Lease`` instance.
        """


@implementer(IFlockerAPIV1Client)
class FakeFlockerClient(object):
    """
    Fake in-memory implementation of ``IFlockerAPIV1Client``.
    """
    # Placeholder time, we don't model the progress of time at all:
    _NOW = datetime.fromtimestamp(0, UTC)

    def __init__(self):
        self._configured_datasets = pmap()
        self._configured_containers = pmap()
        self._leases = LeasesModel()
        addresses = iter(IPNetwork("10.0.0.0/8"))
        self._nodes = pset(
            Node(uuid=uuid4(), public_address=next(addresses))
            for i in range(2)
        )
        self.synchronize_state()

    def version(self):
        return succeed({u"flocker": u"1.2.3.4"})

    def list_nodes(self):
        return succeed(list(self._nodes))

    def create_container(self, node_uuid, name, image):
        if name in self._configured_containers:
            return fail(ContainerAlreadyExists())
        result = Container(node_uuid=node_uuid, name=name, image=image)
        self._configured_containers = self._configured_containers.set(
            name, result
        )
        return succeed(result)

    def list_containers_state(self):
        return succeed(self._state_containers)

    def create_dataset(self, primary, maximum_size=None, dataset_id=None,
                       metadata=pmap()):
        # In real implementation the server will generate the new ID, but
        # we have to do it ourselves:
        if dataset_id is None:
            dataset_id = uuid4()
        if dataset_id in self._configured_datasets:
            return fail(DatasetAlreadyExists())
        result = Dataset(primary=primary, maximum_size=maximum_size,
                         dataset_id=dataset_id, metadata=metadata)
        self._configured_datasets = self._configured_datasets.set(
            dataset_id, result)
        return succeed(result)

    def delete_dataset(self, dataset_id):
        dataset = self._configured_datasets[dataset_id]
        self._configured_datasets = self._configured_datasets.remove(
            dataset_id)
        return succeed(dataset)

    def move_dataset(self, primary, dataset_id):
        self._configured_datasets = self._configured_datasets.transform(
            [dataset_id, "primary"], primary)
        return succeed(self._configured_datasets[dataset_id])

    def list_datasets_configuration(self):
        return succeed(self._configured_datasets.values())

    def list_datasets_state(self):
        return succeed(self._state_datasets)

    def synchronize_state(self):
        """
        Copy configuration into state.
        """
        self._state_datasets = [
            DatasetState(
                dataset_id=dataset.dataset_id,
                primary=dataset.primary,
                maximum_size=dataset.maximum_size,
                path=FilePath(b"/flocker").child(bytes(dataset.dataset_id)))
            for dataset in self._configured_datasets.values()]
        self._state_containers = list(
            ContainerState(**container.serialize())
            for container in self._configured_containers.values()
        )

    def acquire_lease(self, dataset_id, node_uuid, expires):
        try:
            self._leases = self._leases.acquire(
                self._NOW, dataset_id, node_uuid, expires)
        except LeaseError:
            return fail(LeaseAlreadyHeld())
        return succeed(
            Lease(dataset_id=dataset_id, node_uuid=node_uuid, expires=expires))

    def release_lease(self, dataset_id):
        # We don't handle the case where lease doesn't exist yet, since
        # it's not clear that's necessary yet. If it is we'll need to
        # expand this logic.
        lease = self._leases[dataset_id]
        self._leases = self._leases.release(dataset_id, lease.node_id)
        return succeed(
            Lease(dataset_id=dataset_id, node_uuid=lease.node_id,
                  expires=((lease.expiration - self._NOW).total_seconds()
                           if lease.expiration is not None else None)))

    def list_leases(self):
        return succeed([
            Lease(dataset_id=l.dataset_id, node_uuid=l.node_id,
                  expires=((l.expiration - self._NOW).total_seconds()
                           if l.expiration is not None else None))
            for l in self._leases.values()])


class ResponseError(Exception):
    """
    An unexpected response from the REST API.
    """
    def __init__(self, code, body):
        Exception.__init__(self, "Unexpected response code {}:\n{}\n".format(
            code, body))
        self.code = code


@implementer(IFlockerAPIV1Client)
class FlockerClient(object):
    """
    A client for the Flocker V1 REST API.
    """
    def __init__(self, reactor, host, port,
                 ca_cluster_path, cert_path, key_path):
        """
        :param reactor: Reactor to use for connections.
        :param bytes host: Host to connect to.
        :param int port: Port to connect to:
        :param FilePath ca_cluster_path: Path to cluster's CA certificate.
        :param FilePath cert_path: Path to user certificate.
        :param FilePath key_path: Path to user private key.
        """
        self._treq = treq_with_authentication(reactor, ca_cluster_path,
                                              cert_path, key_path)
        self._base_url = b"https://%s:%d/v1" % (host, port)

    def _request(self, method, path, body, success_codes, error_codes=None):
        """
        Send a HTTP request to the Flocker API, return decoded JSON body.

        :param bytes method: HTTP method, e.g. PUT.
        :param bytes path: Path to add to base URL.
        :param body: If not ``None``, JSON encode this and send as the
            body of the request.
        :param set success_codes: Expected success response codes.
        :param error_codes: Mapping from HTTP response code to exception to be
            raised if it is present, or ``None`` to send no headers.

        :return: ``Deferred`` firing with decoded JSON.
        """
        url = self._base_url + path
        action = _LOG_HTTP_REQUEST(url=url, method=method, request_body=body)

        if error_codes is None:
            error_codes = {}

        def error(body, code):
            if code in error_codes:
                raise error_codes[code](body)
            raise ResponseError(code, body)

        def got_result(result):
            if result.code in success_codes:
                action.addSuccessFields(response_code=result.code)
                return json_content(result)
            else:
                d = content(result)
                d.addCallback(error, result.code)
                return d

        # Serialize the current task ID so we can trace logging across
        # processes:
        headers = {b"X-Eliot-Task-Id": action.serialize_task_id()}
        data = None
        if body is not None:
            headers["content-type"] = b"application/json"
            data = dumps(body)

        with action.context():
            request = DeferredContext(self._treq.request(
                method, url,
                data=data, headers=headers,
                # Keep tests from having dirty reactor problems:
                persistent=False
                ))
        request.addCallback(got_result)

        def got_body(json_body):
            action.addSuccessFields(response_body=json_body)
            return json_body
        request.addCallback(got_body)
        request.addActionFinish()
        return request.result

    def version(self):
        return self._request(b"GET", b"/version", None, {OK})

    def list_nodes(self):
        d = self._request(b"GET", b"/state/nodes", None, {OK})
        d.addCallback(
            lambda nodes: list(
                Node(
                    uuid=UUID(node[u"uuid"]),
                    public_address=IPAddress(node[u"host"]),
                )
                for node in nodes
            )
        )
        return d

    def create_container(self, primary, name, image):
        container = dict(
            node_uuid=unicode(primary.uuid), name=name, image=image,
        )
        d = self._request(
            b"POST",
            b"/configuration/containers",
            container,
            {CREATED},
            {CONFLICT: ContainerAlreadyExists},
        )
        d.addCallback(self._parse_configuration_container)
        return d

    def list_containers_state(self):
        d = self._request(
            b"GET",
            b"/state/containers",
            None,
            {OK},
        )

        def parse_container_state(container):
            return ContainerState(
                node_uuid=UUID(container[u"node_uuid"]),
                name=container[u"name"],
                image=DockerImage.from_string(container[u"image"]),
            )

        d.addCallback(
            lambda containers: list(
                parse_container_state(container)
                for container in containers
            )
        )
        return d

    def _parse_configuration_container(self, container_dict):
        """
        Convert a dictionary decoded from JSON with a dataset's configuration.

        :param container_dict: Dictionary describing a container.
        :return: ``Container`` instance.
        """
        return Container(
            node_uuid=UUID(container_dict[u"node_uuid"]),
            name=container_dict[u"name"],
            image=DockerImage.from_string(container_dict[u"image"]),
        )

    def _parse_configuration_dataset(self, dataset_dict):
        """
        Convert a dictionary decoded from JSON with a dataset's configuration.

        :param dataset_dict: Dictionary describing a dataset.
        :return: ``Dataset`` instance.
        """
        return Dataset(primary=UUID(dataset_dict[u"primary"]),
                       maximum_size=dataset_dict.get(u"maximum_size", None),
                       dataset_id=UUID(dataset_dict[u"dataset_id"]),
                       metadata=dataset_dict[u"metadata"])

    def delete_dataset(self, dataset_id):
        request = self._request(
            b"DELETE", b"/configuration/datasets/%s" % (dataset_id,),
            None, {OK})
        request.addCallback(self._parse_configuration_dataset)
        return request

    def create_dataset(self, primary, maximum_size=None, dataset_id=None,
                       metadata=pmap()):
        dataset = {u"primary": unicode(primary),
                   u"metadata": dict(metadata)}
        if dataset_id is not None:
            dataset[u"dataset_id"] = unicode(dataset_id)
        if maximum_size is not None:
            dataset[u"maximum_size"] = maximum_size
        request = self._request(b"POST", b"/configuration/datasets",
                                dataset, {CREATED},
                                {CONFLICT: DatasetAlreadyExists})
        request.addCallback(self._parse_configuration_dataset)
        return request

    def move_dataset(self, primary, dataset_id):
        request = self._request(
            b"POST", b"/configuration/datasets/%s" % (dataset_id,),
            {u"primary": unicode(primary)}, {OK})
        request.addCallback(self._parse_configuration_dataset)
        return request

    def list_datasets_configuration(self):
        request = self._request(b"GET", b"/configuration/datasets", None, {OK})
        request.addCallback(
            lambda results:
            [
                self._parse_configuration_dataset(d)
                for d in results if not d['deleted']
            ]
        )
        return request

    def list_datasets_state(self):
        request = self._request(b"GET", b"/state/datasets", None, {OK})

        def parse_dataset_state(dataset_dict):
            primary = dataset_dict.get(u"primary")
            if primary is not None:
                primary = UUID(primary)
            path = dataset_dict.get(u"path")
            if path is not None:
                path = FilePath(path)
            return DatasetState(primary=primary,
                                maximum_size=dataset_dict.get(
                                    u"maximum_size", None),
                                dataset_id=UUID(dataset_dict[u"dataset_id"]),
                                path=path)

        request.addCallback(
            lambda results: [parse_dataset_state(d) for d in results])
        return request

    def _parse_lease(self, dictionary):
        """
        Parse a result dictionary into a ``Lease``.

        :param dict dictionary: API JSON result.
        :return: Corresponding ``Lease``.
        """
        return Lease(dataset_id=UUID(dictionary[u"dataset_id"]),
                     node_uuid=UUID(dictionary[u"node_uuid"]),
                     expires=dictionary[u"expires"])

    def acquire_lease(self, dataset_id, node_uuid, expires=None):
        request = self._request(b"POST", b"/configuration/leases",
                                {u"dataset_id": unicode(dataset_id),
                                 u"node_uuid": unicode(node_uuid),
                                 u"expires": expires},
                                {OK, CREATED},
                                {CONFLICT: LeaseAlreadyHeld})
        request.addCallback(self._parse_lease)
        return request

    def release_lease(self, dataset_id):
        request = self._request(
            b"DELETE", b"/configuration/leases/" + bytes(dataset_id),
            None, {OK})
        request.addCallback(self._parse_lease)
        return request

    def list_leases(self):
        request = self._request(
            b"GET", b"/configuration/leases", None, {OK})
        request.addCallback(
            lambda results: [self._parse_lease(l) for l in results])
        return request
