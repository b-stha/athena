"""Runtime-test doubles; core tests do not depend on external developer tools."""

class SimulatedHA:
    def __init__(self):
        self.actions = []

    async def execute(self, action):
        self.actions.append(action)

    async def preflight(self, action):
        pass

    async def close(self):
        pass


class SimulatedCloud:
    def __init__(self):
        self.requests = []
        self.model_request_attempts = 0

    def remember_local_turn(self, *args, **kwargs):
        pass

    async def turn(self, text, session_id='default'):
        self.requests.append(text)
        self.model_request_attempts += 1
        return 'Simulated contextual result; no model was contacted.'

    async def close(self):
        pass
