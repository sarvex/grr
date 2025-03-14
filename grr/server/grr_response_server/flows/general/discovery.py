#!/usr/bin/env python
"""These are flows designed to discover information about the host."""

import logging
from typing import Any

from google.protobuf import any_pb2
from grr_response_core import config
from grr_response_core.lib import rdfvalue
from grr_response_core.lib.rdfvalues import client as rdf_client
from grr_response_core.lib.rdfvalues import client_fs as rdf_client_fs
from grr_response_core.lib.rdfvalues import cloud as rdf_cloud
from grr_response_core.lib.rdfvalues import protodict as rdf_protodict
from grr_response_core.lib.rdfvalues import structs as rdf_structs
from grr_response_core.stats import metrics
from grr_response_proto import flows_pb2
from grr_response_server import artifact
from grr_response_server import client_index
from grr_response_server import data_store
from grr_response_server import events
from grr_response_server import fleetspeak_utils
from grr_response_server import flow
from grr_response_server import flow_base
from grr_response_server import flow_responses
from grr_response_server import notification
from grr_response_server import server_stubs
from grr_response_server.databases import db
from grr_response_server.flows.general import collectors
from grr_response_server.flows.general import crowdstrike
from grr_response_server.rdfvalues import objects as rdf_objects
from grr_response_proto import rrg_pb2
from grr_response_proto.rrg import os_pb2 as rrg_os_pb2
from grr_response_proto.rrg.action import get_system_metadata_pb2 as rrg_get_system_metadata_pb2

FLEETSPEAK_UNLABELED_CLIENTS = metrics.Counter("fleetspeak_unlabeled_clients")
CLOUD_METADATA_COLLECTION_ERRORS = metrics.Counter(
    "cloud_metadata_collection_errors")


class InterrogateArgs(rdf_structs.RDFProtoStruct):
  protobuf = flows_pb2.InterrogateArgs


class Interrogate(flow_base.FlowBase):
  """Interrogate various things about the host."""

  category = "/Administrative/"
  client = None
  args_type = InterrogateArgs
  result_types = (rdf_client.ClientSummary,)
  behaviours = flow_base.BEHAVIOUR_BASIC

  def Start(self):
    """Start off all the tests."""
    self.state.client = rdf_objects.ClientSnapshot(client_id=self.client_id)
    self.state.client.metadata.source_flow_id = self.rdf_flow.flow_id
    self.state.fqdn = None
    self.state.os = None

    # There are three possible scenarios:
    #
    #   1. Only Python agent is supported.
    #   2. Only RRG is supported.
    #   3. Both RRG and Python agent are supported.
    #
    # Additionally, for backward compatibility we assume that if RRG is not
    # explicitly supported, then GRR is definitely supported. This assumption
    # may be revisited in the future.
    #
    # Anyway, if both agents are supported we need to get metadata about both
    # of them. If only one is supported we need to get metadata about that one
    # and only that one. It is important not to issue the request to the other
    # one as the flow will get stuck awaiting the response to come.
    if self.rrg_support:
      if self.python_agent_support:
        self.CallClient(
            server_stubs.GetClientInfo,
            next_state=self.ClientInfo.__name__,
        )
      self.CallRRG(
          action=rrg_pb2.Action.GET_SYSTEM_METADATA,
          args=rrg_get_system_metadata_pb2.Args(),
          next_state=self.HandleRRGGetSystemMetadata.__name__,
      )
    else:
      # ClientInfo should be collected early on since we might need the client
      # version later on to know what actions a client supports.
      self.CallClient(
          server_stubs.GetClientInfo, next_state=self.ClientInfo.__name__
      )
      self.CallClient(
          server_stubs.GetPlatformInfo, next_state=self.Platform.__name__
      )
      self.CallClient(
          server_stubs.GetInstallDate, next_state=self.InstallDate.__name__
      )

    self.CallClient(
        server_stubs.GetMemorySize, next_state=self.StoreMemorySize.__name__)
    self.CallClient(
        server_stubs.GetConfiguration,
        next_state=self.ClientConfiguration.__name__)
    self.CallClient(
        server_stubs.GetLibraryVersions,
        next_state=self.ClientLibraries.__name__)
    self.CallClient(
        server_stubs.EnumerateInterfaces,
        next_state=self.EnumerateInterfaces.__name__)
    self.CallClient(
        server_stubs.EnumerateFilesystems,
        next_state=self.EnumerateFilesystems.__name__)

    if config.CONFIG["Interrogate.collect_crowdstrike_agent_id"]:
      self.CallFlow(
          crowdstrike.GetCrowdStrikeAgentID.__name__,
          next_state=self.ProcessGetCrowdStrikeAgentID.__name__,
      )

  @flow_base.UseProto2AnyResponses
  def HandleRRGGetSystemMetadata(
      self,
      responses: flow_responses.Responses[any_pb2.Any],
  ) -> None:
    """Handles responses from RRG `GET_SYSTEM_METADATA` action."""
    if not responses.success:
      message = f"RRG system metadata collection failed: {responses.status}"
      raise flow_base.FlowError(message)

    if len(responses) != 1:
      message = (
          "Unexpected number of RRG system metadata responses: "
          f"{len(responses)}"
      )
      raise flow_base.FlowError(message)

    result = rrg_get_system_metadata_pb2.Result()
    result.ParseFromString(list(responses)[0].value)

    if result.type == rrg_os_pb2.Type.LINUX:
      self.state.client.knowledge_base.os = "Linux"
    elif result.type == rrg_os_pb2.Type.MACOS:
      self.state.client.knowledge_base.os = "Darwin"
    elif result.type == rrg_os_pb2.Type.WINDOWS:
      self.state.client.knowledge_base.os = "Windows"
    else:
      self.Log("Unexpected operating system: %s", result.type)

    self.state.client.knowledge_base.fqdn = result.fqdn
    self.state.client.os_version = result.version
    self.state.client.install_time = rdfvalue.RDFDatetime.FromDatetime(
        result.install_time.ToDatetime()
    )

    # TODO: Remove these lines.
    #
    # At the moment `ProcessKnowledgeBase` uses `state.fqdn` and `state.os` for
    # knowledgebase and overrides everything else. The code should be refactored
    # to merge knowledgebase from individual client calls and the knowledgebase
    # initialization flow.
    self.state.fqdn = self.state.client.knowledge_base.fqdn
    self.state.os = self.state.client.knowledge_base.os

    # We should not assume that the GRR client is running if we got responses
    # from RRG. It means, that GRR-only requests will never be delivered and
    # the flow will get stuck never reaching executing it's `End` method. To
    # still preserve information in such cases, we write the snapshot right now.
    # If GRR is running and adds more complete data, this information will be
    # just overridden.
    data_store.REL_DB.WriteClientSnapshot(self.state.client)

    # Cloud VM metadata collection is not supported in RRG at the moment but we
    # still need it, so we fall back to the Python agent. This is the same call
    # that we make in the `Interrogate.Platform` method handler.
    if result.type in [rrg_os_pb2.Type.LINUX, rrg_os_pb2.Type.WINDOWS]:
      self.CallClient(
          server_stubs.GetCloudVMMetadata,
          rdf_cloud.BuildCloudMetadataRequests(),
          next_state=self.CloudMetadata.__name__,
      )

    # We replicate behaviour of Python-based agents: once the operating system
    # is known, we can start the knowledgebase initialization flow.
    if self.state.client.knowledge_base.os:
      args = artifact.KnowledgeBaseInitializationArgs()
      args.require_complete = False  # Not all dependencies are known yet.
      args.lightweight = self.args.lightweight

      self.CallFlow(
          flow_name=artifact.KnowledgeBaseInitializationFlow.__name__,
          flow_args=args,
          next_state=self.ProcessKnowledgeBase.__name__,
      )

  def CloudMetadata(self, responses):
    """Process cloud metadata and store in the client."""
    if not responses.success:
      CLOUD_METADATA_COLLECTION_ERRORS.Increment()

      # We want to log this but it's not serious enough to kill the whole flow.
      self.Log("Failed to collect cloud metadata: %s" % responses.status)
      return
    metadata_responses = responses.First()

    # Expected for non-cloud machines.
    if not metadata_responses:
      return

    convert = rdf_cloud.ConvertCloudMetadataResponsesToCloudInstance

    client = self.state.client
    client.cloud_instance = convert(metadata_responses)

  def StoreMemorySize(self, responses):
    """Stores the memory size."""
    if not responses.success:
      return

    self.state.client.memory_size = responses.First()

  def Platform(self, responses):
    """Stores information about the platform."""
    if responses.success:
      response = responses.First()

      client = self.state.client
      client.os_release = response.release
      client.os_version = response.version
      client.kernel = response.kernel
      client.arch = response.machine
      client.knowledge_base.os = response.system
      # Store these for later, there might be more accurate data
      # coming in from the artifact collector.
      self.state.fqdn = response.fqdn
      self.state.os = response.system

      existing_client = data_store.REL_DB.ReadClientSnapshot(self.client_id)
      if existing_client is None:
        # This is the first time we interrogate this client. In that case, we
        # need to store basic information about this client right away so
        # follow up flows work properly.
        data_store.REL_DB.WriteClientSnapshot(self.state.client)

      try:
        # Update the client index
        client_index.ClientIndex().AddClient(client)
      except db.UnknownClientError:
        pass

      # No support for OS X cloud machines as yet.
      if response.system in ["Linux", "Windows"]:
        self.CallClient(
            server_stubs.GetCloudVMMetadata,
            rdf_cloud.BuildCloudMetadataRequests(),
            next_state=self.CloudMetadata.__name__)

      known_system_type = True
    else:
      # We failed to get the Platform info, maybe there is a stored
      # system we can use to get at least some data.
      client = data_store.REL_DB.ReadClientSnapshot(self.client_id)
      known_system_type = client and client.knowledge_base.os

      self.Log("Could not retrieve Platform info.")

    if known_system_type:
      # We will accept a partial KBInit rather than raise, so pass
      # require_complete=False.
      self.CallFlow(
          artifact.KnowledgeBaseInitializationFlow.__name__,
          require_complete=False,
          lightweight=self.args.lightweight,
          next_state=self.ProcessKnowledgeBase.__name__)
    else:
      self.Log("Unknown system type, skipping KnowledgeBaseInitializationFlow")

  def InstallDate(self, responses):
    """Stores the time when the OS was installed on the client."""
    if not responses.success:
      self.Log("Could not get InstallDate")
      return

    response = responses.First()

    # When using relational flows, the response is serialized as an any value
    # and we get an equivalent RDFInteger here so we need to check for both.
    if isinstance(response, (rdfvalue.RDFDatetime, rdfvalue.RDFInteger)):
      # New clients send the correct values already.
      install_date = response
    elif isinstance(response, rdf_protodict.DataBlob):
      # For backwards compatibility.
      install_date = rdfvalue.RDFDatetime.FromSecondsSinceEpoch(
          response.integer)
    else:
      self.Log("Unknown response type for InstallDate: %s" % type(response))
      return

    self.state.client.install_time = install_date

  def ProcessKnowledgeBase(self, responses):
    """Collect and store any extra non-kb artifacts."""
    if not responses.success:
      raise flow_base.FlowError(
          "Error while collecting the knowledge base: %s" % responses.status)

    kb = responses.First()

    # Information already present in the knowledge base takes precedence.
    if not kb.os:
      kb.os = self.state.os

    if not kb.fqdn:
      kb.fqdn = self.state.fqdn

    self.state.client.knowledge_base = kb

    non_kb_artifacts = config.CONFIG["Artifacts.non_kb_interrogate_artifacts"]
    if non_kb_artifacts:
      self.CallFlow(
          collectors.ArtifactCollectorFlow.__name__,
          artifact_list=non_kb_artifacts,
          knowledge_base=kb,
          next_state=self.ProcessArtifactResponses.__name__)

    try:
      # Update the client index for the rdf_objects.ClientSnapshot.
      client_index.ClientIndex().AddClient(self.state.client)
    except db.UnknownClientError:
      pass

  def ProcessArtifactResponses(self, responses):
    if not responses.success:
      self.Log("Error collecting artifacts: %s", responses.status)
    if not list(responses):
      return

    for response in responses:
      if isinstance(response, rdf_client_fs.Volume):
        self.state.client.volumes.append(response)
      elif isinstance(response, rdf_client.HardwareInfo):
        self.state.client.hardware_info = response
      else:
        raise ValueError("Unexpected response type: %s" % type(response))

  FILTERED_IPS = ["127.0.0.1", "::1", "fe80::1"]

  def EnumerateInterfaces(self, responses):
    """Enumerates the interfaces."""
    if not (responses.success and responses):
      self.Log("Could not enumerate interfaces: %s" % responses.status)
      return

    self.state.client.interfaces = sorted(responses, key=lambda i: i.ifname)

  def EnumerateFilesystems(self, responses):
    """Store all the local filesystems in the client."""
    if not responses.success or not responses:
      self.Log("Could not enumerate file systems.")
      return

    # rdf_objects.ClientSnapshot.
    self.state.client.filesystems = responses

  def _ValidateLabel(self, label):
    if not label:
      raise ValueError("Label name cannot be empty.")

    is_valid = lambda char: char.isalnum() or char in " _./:-"
    if not all(map(is_valid, label)):
      raise ValueError("Label name can only contain: "
                       "a-zA-Z0-9_./:- but got: '%s'" % label)

  def ClientInfo(self, responses):
    """Obtain some information about the GRR client running."""
    if not responses.success:
      self.Log("Could not get ClientInfo.")
      return

    response = responses.First()

    # Fetch labels for the client from Fleetspeak. If Fleetspeak doesn't
    # have any labels for the GRR client, fall back to labels reported by
    # the client.
    fleetspeak_labels = fleetspeak_utils.GetLabelsFromFleetspeak(self.client_id)
    if fleetspeak_labels:
      response.labels = fleetspeak_labels
      data_store.REL_DB.AddClientLabels(
          client_id=self.client_id, owner="GRR", labels=fleetspeak_labels
      )
    else:
      FLEETSPEAK_UNLABELED_CLIENTS.Increment()
      logging.warning(
          "Failed to get labels for Fleetspeak client %s.", self.client_id
      )

    sanitized_labels = []
    for label in response.labels:
      try:
        self._ValidateLabel(label)
        sanitized_labels.append(label)
      except ValueError:
        self.Log("Got invalid label: %s", label)

    response.labels = sanitized_labels

    self.state.client.startup_info.client_info = response

    metadata = data_store.REL_DB.ReadClientMetadata(self.client_id)
    if metadata and metadata.last_fleetspeak_validation_info:
      self.state.client.fleetspeak_validation_info = (
          metadata.last_fleetspeak_validation_info)

  def ClientConfiguration(self, responses):
    """Process client config."""
    if not responses.success:
      return

    response = responses.First()

    for k, v in response.items():
      self.state.client.grr_configuration.Append(key=k, value=str(v))

  def ClientLibraries(self, responses):
    """Process client library information."""
    if not responses.success:
      return

    response = responses.First()
    for k, v in response.items():
      self.state.client.library_versions.Append(key=k, value=str(v))

  def ProcessGetCrowdStrikeAgentID(
      self,
      responses: flow_responses.Responses[Any],
  ) -> None:
    if not responses.success:
      status = responses.status
      self.Log("failed to obtain CrowdStrike agent identifier: %s", status)
      return

    for response in responses:
      if not isinstance(response, crowdstrike.GetCrowdstrikeAgentIdResult):
        raise TypeError(f"Unexpected response type: {type(response)}")

      edr_agent = rdf_client.EdrAgent()
      edr_agent.name = "CrowdStrike"
      edr_agent.agent_id = response.agent_id
      self.state.client.edr_agents.append(edr_agent)

  def NotifyAboutEnd(self):
    notification.Notify(
        self.creator,
        rdf_objects.UserNotification.Type.TYPE_CLIENT_INTERROGATED, "",
        rdf_objects.ObjectReference(
            reference_type=rdf_objects.ObjectReference.Type.CLIENT,
            client=rdf_objects.ClientReference(client_id=self.client_id)))

  def End(self, responses):
    """Finalize client registration."""
    # Update summary and publish to the Discovery queue.
    del responses

    try:
      data_store.REL_DB.WriteClientSnapshot(self.state.client)
    except db.UnknownClientError:
      pass

    summary = self.state.client.GetSummary()
    summary.client_id = self.client_id
    summary.timestamp = rdfvalue.RDFDatetime.Now()
    summary.last_ping = summary.timestamp

    events.Events.PublishEvent("Discovery", summary, username=self.creator)

    self.SendReply(summary)

    index = client_index.ClientIndex()
    index.AddClient(self.state.client)
    labels = self.state.client.startup_info.client_info.labels
    if labels:
      data_store.REL_DB.AddClientLabels(self.state.client.client_id, "GRR",
                                        labels)

    # Reset foreman rules check so active hunts can match against the new data
    data_store.REL_DB.WriteClientMetadata(
        self.client_id,
        last_foreman=data_store.REL_DB.MinTimestamp(),
    )


class EnrolmentInterrogateEvent(events.EventListener):
  """An event handler which will schedule interrogation on client enrollment."""
  EVENTS = ["ClientEnrollment"]

  def ProcessEvents(self, msgs=None, publisher_username=None):
    for msg in msgs:
      flow.StartFlow(
          client_id=msg.Basename(),
          flow_cls=Interrogate,
          creator=publisher_username)
