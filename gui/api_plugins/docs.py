#!/usr/bin/env python
"""API renderer for rendering API docs."""

from grr.gui import api_aff4_object_renderers
from grr.gui import api_call_renderers
from grr.lib import registry


class ApiDocsRenderer(api_call_renderers.ApiCallRenderer):
  """Renders HTTP API docs sources."""

  def RenderApiCallRenderers(self, routing_rules):
    return {
        rule.rule: dict(
            route=rule.rule,
            renderer=rule.endpoint.__name__,
            methods=list(rule.methods - {"HEAD"}),
            doc=rule.endpoint.__doc__,
            args_type=(rule.endpoint.args_type
                       and rule.endpoint.args_type.__name__),
        )
        for rule in routing_rules
    }

  def RenderApiObjectRenderers(self, renderers):
    return {
        renderer.aff4_type: dict(
            name=renderer.__name__,
            doc=renderer.__doc__,
            args_type=renderer.args_type and renderer.args_type.__name__,
        )
        for renderer in renderers if renderer.aff4_type
    }

  def Render(self, unused_args, token=None):
    routing_rules = sorted(
        api_call_renderers.HTTP_ROUTING_MAP.iter_rules(),
        key=lambda x: x.rule)

    object_renderers = (api_aff4_object_renderers.ApiAFF4ObjectRenderer.
                        classes.values())

    return dict(
        api_call_renderers=self.RenderApiCallRenderers(routing_rules),
        api_object_renderers=self.RenderApiObjectRenderers(object_renderers)
        )


class ApiDocsInitHook(registry.InitHook):

  def RunOnce(self):
    api_call_renderers.RegisterHttpRouteHandler(
        "GET", "/api/docs", ApiDocsRenderer)
