"""Synchronous diagnostics using the same HA MCP executor as voice/text."""

import asyncio


def call_service(domain, service, entity_id=None, *, area_id=None):
    # Compatibility entry point, not a second endpoint or transport.
    from router import Action, validate_action
    from athena_agent.local_ha import HomeAssistantMCP, LocalHAConfig
    if domain != 'light' or bool(entity_id) == bool(area_id):
        raise ValueError('Provide exactly one registered light entity or area.')
    action = validate_action(Action('home_assistant', service,
                                   entity_id or area_id, 'entity_id' if entity_id else 'area_id'))

    async def send():
        backend = HomeAssistantMCP(LocalHAConfig.from_env())
        try:
            await backend.execute(action)
        finally:
            await backend.close()

    asyncio.run(send())
