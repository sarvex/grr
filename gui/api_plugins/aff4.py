#!/usr/bin/env python
"""API renderers for accessing AFF4 objects."""



# pylint: disable=unused-import,g-bad-import-order
from grr.lib import server_plugins
# pylint: enable=unused-import,g-bad-import-order

from grr.gui import api_aff4_object_renderers
from grr.gui import api_call_renderers
from grr.gui import api_value_renderers
from grr.lib import aff4
from grr.lib import data_store
from grr.lib import rdfvalue
from grr.lib import registry
from grr.proto import api_pb2


class ApiAff4RendererArgs(rdfvalue.RDFProtoStruct):
  protobuf = api_pb2.ApiAff4RendererArgs


class ApiAff4Renderer(api_call_renderers.ApiCallRenderer):
  """Renders AFF4 objects in JSON format.

  Query parameters interpretation depends on the type of the AFF4 object
  that's being fetched. See documentation on AFF4 object renderers for
  details.
  """

  args_type = ApiAff4RendererArgs

  @classmethod
  def GetAdditionalArgsTypes(cls):
    return {
        aff4_renderer_cls.aff4_type: aff4_renderer_cls.args_type
        for aff4_renderer_cls in (
            api_aff4_object_renderers.ApiAFF4ObjectRendererBase.classes.values())
    }

  additional_args_types = GetAdditionalArgsTypes

  def Render(self, args, token=None):
    aff4_object = aff4.FACTORY.Open(args.aff4_path, token=token)
    return api_aff4_object_renderers.RenderAFF4Object(
        aff4_object, [x.args for x in args.additional_args])


class ApiAff4IndexRendererArgs(rdfvalue.RDFProtoStruct):
  protobuf = api_pb2.ApiAff4IndexRendererArgs


class ApiAff4IndexRenderer(api_call_renderers.ApiCallRenderer):
  """Returns list of children objects for the object with a given path."""

  args_type = ApiAff4IndexRendererArgs

  def Render(self, args, token=None):
    encoded_urns = []

    aff4_path = rdfvalue.RDFURN(args.aff4_path)
    index_prefix = "index:dir/"
    for predicate, _, timestamp in data_store.DB.ResolveRegex(aff4_path, f"{index_prefix}.+", token=token, timestamp=data_store.DB.NEWEST_TIMESTAMP, limit=1000000):

      urn = aff4_path.Add(predicate[len(index_prefix):])
      encoded_urns.append([api_value_renderers.RenderValue(urn),
                           timestamp])

    return encoded_urns


class ApiAff4InitHook(registry.InitHook):

  def RunOnce(self):
    api_call_renderers.RegisterHttpRouteHandler(
        "GET", "/api/aff4/<path:aff4_path>",
        ApiAff4Renderer)
    api_call_renderers.RegisterHttpRouteHandler(
        "GET", "/api/aff4-index/<path:aff4_path>",
        ApiAff4IndexRenderer)
