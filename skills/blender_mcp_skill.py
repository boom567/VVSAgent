import socket
import json


def register(agent):
    def blender_mcp(
        command_type: str = "get_scene_info",
        params: str = "{}",
        host: str = "localhost",
        port: int = 9876,
    ):
        """Send a command to the BlenderMCP server via TCP socket and return the response."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10.0)
            sock.connect((host, port))

            if isinstance(params, str):
                try:
                    params_dict = json.loads(params)
                except json.JSONDecodeError:
                    params_dict = {}
            else:
                params_dict = params

            payload = {
                "type": command_type,
                "params": params_dict,
            }
            message = json.dumps(payload) + "
"
            sock.sendall(message.encode("utf-8"))

            response_data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response_data += chunk
                if b"
" in chunk:
                    break

            sock.close()

            response_text = response_data.decode("utf-8").strip()
            if not response_text:
                return "Received empty response from BlenderMCP server."

            try:
                response_json = json.loads(response_text)
                return json.dumps(response_json, indent=2, ensure_ascii=False)
            except json.JSONDecodeError:
                return response_text

        except socket.timeout:
            return f"Timeout: No response from BlenderMCP server at {host}:{port} within 10 seconds."
        except ConnectionRefusedError:
            return (
                f"Connection refused at {host}:{port}. Make sure Blender is running "
                f"with the BlenderMCP addon enabled and the server is started "
                f"(click 'Connect to Claude' in Blender's sidebar)."
            )
        except Exception as e:
            return f"Error communicating with BlenderMCP: {str(e)}"

    agent.add_skill(
        name="blender_mcp",
        func=blender_mcp,
        description=("Send a command to the BlenderMCP server running inside Blender. "
                     "Connects via TCP socket (default localhost:9876). "
                     "Common command types: get_scene_info, create_object, delete_object, modify_object, "
                     "apply_material, execute_blender_code, get_scene_hierarchy, etc. "
                     "Params should be a JSON string of parameters for the command."),
        parameters={
            "command_type": {"type": "string", "description": "command type"},
            "params": {"type": "string", "description": "JSON params"},
            "host": {"type": "string", "description": "server host"},
            "port": {"type": "integer", "description": "server port"},
        },
    )
