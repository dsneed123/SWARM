from swarm.tools.builtin.execute import PythonTool, ShellTool
from swarm.tools.builtin.external import GitHubTool, HttpRequestTool
from swarm.tools.builtin.files import ListFilesTool, ReadFileTool, WriteFileTool
from swarm.tools.builtin.web import WebFetchTool, WebSearchTool
from swarm.tools.registry import Tool


def builtin_tools() -> list[Tool]:
    return [
        WebSearchTool(), WebFetchTool(),
        ReadFileTool(), WriteFileTool(), ListFilesTool(),
        PythonTool(), ShellTool(),
        HttpRequestTool(), GitHubTool(),
    ]
