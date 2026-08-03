#!/usr/bin/env python3
"""
AI-Effect Onboarding Export Generator

Generates an AI-Effect onboarding export package from a use case directory
containing services/ with proto files and a connections.json defining the
pipeline topology.

Creates: blueprint.json, dockerinfo.json, generation_metadata.json,
and microservice/*.proto files.

Required input structure:
    use-case-dir/
        services/
            <service_dir>/proto/<name>.proto
        connections.json   (pipeline topology + service_mapping)
"""

import json
import shutil
import argparse
import zipfile
from pathlib import Path
from datetime import datetime
import uuid


class OnboardingExportGenerator:
    def __init__(self, use_case_dir, output_dir):
        self.use_case_dir = Path(use_case_dir)
        self.output_dir = Path(output_dir)
        self.services_dir = self.use_case_dir / 'services'

    def scan_services(self, service_mapping):
        """Scan services directory and extract service information.

        Uses service_mapping from connections.json to resolve the Docker
        service name (ip_address) for each service directory.
        """
        services = []

        if not self.services_dir.exists():
            raise FileNotFoundError(f"Services directory not found: {self.services_dir}")

        for service_dir in sorted(self.services_dir.iterdir()):
            if not service_dir.is_dir():
                continue

            proto_dir = service_dir / 'proto'
            if not proto_dir.exists():
                print(f"Warning: No proto directory found in {service_dir}")
                continue

            proto_files = list(proto_dir.glob('*.proto'))
            if not proto_files:
                print(f"Warning: No proto file found in {proto_dir}")
                continue

            dir_name = service_dir.name
            mapping = service_mapping.get(dir_name)
            if not mapping:
                raise ValueError(
                    f"Service directory '{dir_name}' not found in connections.json service_mapping. "
                    f"Available: {list(service_mapping.keys())}"
                )

            service_info = {
                'name': mapping['ip_address'],
                'dir_name': dir_name,
                'container_name': f"{dir_name}1",
                'proto_file': proto_files[0],
                'service_dir': service_dir,
            }
            services.append(service_info)
            print(f"Found service: {dir_name} -> {mapping['ip_address']}:{mapping['port']}")

        return services

    def extract_rpc_methods(self, proto_file):
        """Extract RPC method information from proto file"""
        with open(proto_file, 'r') as f:
            content = f.read()

        rpc_methods = []
        for line in content.split('\n'):
            line = line.strip()
            if line.startswith('rpc '):
                # Parse: rpc MethodName(InputType) returns (OutputType);
                parts = line.replace('rpc ', '').replace(';', '').split('returns')
                if len(parts) == 2:
                    method_part = parts[0].strip()
                    return_part = parts[1].strip()

                    method_name = method_part.split('(')[0].strip()
                    input_msg = method_part.split('(')[1].split(')')[0].strip()
                    output_msg = return_part.strip('()').strip()

                    rpc_methods.append({
                        'method_name': method_name,
                        'input_message': input_msg,
                        'output_message': output_msg
                    })

        return rpc_methods

    def load_connections(self):
        """Load connections.json (required).

        Returns:
            tuple: (connections_list, service_mapping_dict)
        """
        connections_file = self.use_case_dir / 'connections.json'

        if not connections_file.exists():
            raise FileNotFoundError(
                f"connections.json not found in {self.use_case_dir}. "
                "This file is required and must include a service_mapping."
            )

        with open(connections_file, 'r') as f:
            connections_config = json.load(f)

        pipeline = connections_config.get('pipeline', {})
        connections = pipeline.get('connections', [])
        service_mapping = pipeline.get('service_mapping')

        if not service_mapping:
            raise ValueError(
                "connections.json must include pipeline.service_mapping with entries for each service directory."
            )

        return connections, service_mapping

    def create_service_connections(self, services, connections_config):
        """Create per-method service connections from connections.json.

        Returns:
            dict: {container_name: {from_method: [connection_targets]}}
                  Uses "__all__" key when from_method is absent.
        """
        connections = {}

        # Map both docker service name (ip_address) and dir name to container name
        service_map = {}
        for svc in services:
            service_map[svc['name']] = svc['container_name']
            service_map[svc['dir_name']] = svc['container_name']

        for conn in connections_config:
            from_service = conn['from_service']
            to_service = conn['to_service']
            to_method = conn['to_method']
            from_method = conn.get('from_method')

            if from_service in service_map and to_service in service_map:
                from_container = service_map[from_service]
                to_container = service_map[to_service]

                if from_container not in connections:
                    connections[from_container] = {}

                method_key = from_method if from_method else "__all__"
                if method_key not in connections[from_container]:
                    connections[from_container][method_key] = []

                connections[from_container][method_key].append({
                    "container_name": to_container,
                    "operation_signature": {
                        "operation_name": to_method
                    }
                })

                print(f"Connection: {from_service}.{from_method or '*'} -> {to_service}.{to_method}")
            else:
                print(f"Warning: Connection references unknown services: {from_service} -> {to_service}")
                print(f"  Available services: {list(service_map.keys())}")

        return connections

    def generate_blueprint_node(self, service, connections, connected_methods, node_type):
        """Generate a blueprint node for a service."""
        container = service['container_name']
        container_conns = connections.get(container, {})

        rpc_info = self.extract_rpc_methods(service['proto_file'])
        operation_signatures = []

        for rpc in rpc_info:
            method_name = rpc['method_name']

            # Filter: if we know which methods are connected, skip unconnected ones
            if connected_methods and method_name not in connected_methods:
                continue

            # Look up connections: try specific method, then fall back to __all__
            method_connections = container_conns.get(method_name, container_conns.get("__all__", []))

            operation_sig = {
                "connected_to": method_connections,
                "operation_signature": {
                    "operation_name": method_name,
                    "output_message_name": rpc['output_message'],
                    "input_message_name": rpc['input_message'],
                    "output_message_stream": False,
                    "input_message_stream": False
                }
            }
            operation_signatures.append(operation_sig)

        node = {
            "proto_uri": f"microservice/{service['container_name']}.proto",
            "image": f"{self.use_case_dir.name}-{service['name']}:latest",
            "node_type": node_type,
            "container_name": service['container_name'],
            "operation_signature_list": operation_signatures
        }

        return node

    def generate_blueprint(self, services, connections_config):
        """Generate blueprint.json"""
        connections = self.create_service_connections(services, connections_config)

        # Build lookup: service name/dir_name -> container_name
        name_to_container = {}
        for svc in services:
            name_to_container[svc['name']] = svc['container_name']
            name_to_container[svc['dir_name']] = svc['container_name']

        # Determine topology (incoming/outgoing) and connected methods per container
        has_outgoing = set()
        has_incoming = set()
        connected_methods_per_container = {}

        for conn in connections_config:
            from_svc = conn.get('from_service', '')
            to_svc = conn.get('to_service', '')
            from_method = conn.get('from_method')
            to_method = conn.get('to_method')

            from_container = name_to_container.get(from_svc)
            to_container = name_to_container.get(to_svc)

            if from_container:
                has_outgoing.add(from_container)
                if from_method:
                    connected_methods_per_container.setdefault(from_container, set()).add(from_method)
            if to_container:
                has_incoming.add(to_container)
                if to_method:
                    connected_methods_per_container.setdefault(to_container, set()).add(to_method)

        nodes = []
        for service in services:
            container = service['container_name']

            # Auto-detect node_type from topology
            is_source = container not in has_incoming
            is_sink = container not in has_outgoing
            if is_source:
                node_type = "DataSource"
            elif is_sink:
                node_type = "DataSink"
            else:
                node_type = "MLModel"

            cm = connected_methods_per_container.get(container, set())
            node = self.generate_blueprint_node(service, connections, cm, node_type)
            nodes.append(node)

        blueprint = {
            "nodes": nodes,
            "name": f"{self.use_case_dir.name.replace('_', ' ').title()}",
            "pipeline_id": str(uuid.uuid4()),
            "creation_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
            "type": "pipeline-topology/v2",
            "version": "2.0"
        }

        return blueprint

    def generate_dockerinfo(self, services, service_mapping):
        """Generate dockerinfo.json from service_mapping.

        Uses ip_address + port from connections.json service_mapping.
        For services on the same Docker network, use Docker DNS names.
        For remote services, use real IPs/hostnames.
        """
        docker_info_list = []

        for service in services:
            mapping = service_mapping[service['dir_name']]

            docker_info = {
                "container_name": service['container_name'],
                "ip_address": mapping['ip_address'],
                "port": str(mapping['port']),
            }
            docker_info_list.append(docker_info)

        return {"docker_info_list": docker_info_list}

    def copy_proto_files(self, services):
        """Copy proto files to microservice directory"""
        microservice_dir = self.output_dir / 'microservice'
        microservice_dir.mkdir(parents=True, exist_ok=True)

        for service in services:
            src_proto = service['proto_file']
            dst_proto = microservice_dir / f"{service['container_name']}.proto"
            shutil.copy2(src_proto, dst_proto)
            print(f"Copied: {src_proto} -> {dst_proto}")

    def generate_metadata(self, services):
        """Generate generation_metadata.json"""
        metadata = {
            "use_case_name": self.use_case_dir.name,
            "use_case_directory": self.use_case_dir.name,
            "source_path": f"../../use-cases/{self.use_case_dir.name}",
            "services": []
        }

        for service in services:
            metadata["services"].append({
                "service_name": service['name'],
                "container_name": service['container_name'],
                "image_name": f"{self.use_case_dir.name}-{service['name']}:latest"
            })

        return metadata

    def create_zip(self, zip_path):
        """Package blueprint.json, dockerinfo.json and microservice/ into a ZIP
        suitable for the portal's Import ZIP action.

        generation_metadata.json is intentionally excluded — it is only used
        locally and not consumed by the portal importer.
        """
        zip_path = Path(zip_path)
        zip_path.parent.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for name in ('blueprint.json', 'dockerinfo.json'):
                path = self.output_dir / name
                if not path.exists():
                    raise FileNotFoundError(f"Expected {path} in output dir")
                zf.write(path, name)

            microservice_dir = self.output_dir / 'microservice'
            if not microservice_dir.exists():
                raise FileNotFoundError(f"Expected {microservice_dir} in output dir")
            for proto in sorted(microservice_dir.glob('*.proto')):
                zf.write(proto, f"microservice/{proto.name}")

        print(f"Packaged: {zip_path}")
        return zip_path

    def generate_export(self):
        """Generate complete onboarding export"""
        print(f"Generating onboarding export from: {self.use_case_dir}")
        print(f"Output directory: {self.output_dir}")

        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Load connections config (required)
        connections_config, service_mapping = self.load_connections()

        # Scan services (uses service_mapping to resolve names)
        services = self.scan_services(service_mapping)
        if not services:
            raise ValueError("No services with proto files found")

        # Generate blueprint.json
        blueprint = self.generate_blueprint(services, connections_config)
        blueprint_file = self.output_dir / 'blueprint.json'
        with open(blueprint_file, 'w') as f:
            json.dump(blueprint, f, indent=4)
        print(f"Generated: {blueprint_file}")

        # Generate dockerinfo.json
        dockerinfo = self.generate_dockerinfo(services, service_mapping)
        dockerinfo_file = self.output_dir / 'dockerinfo.json'
        with open(dockerinfo_file, 'w') as f:
            json.dump(dockerinfo, f, indent=4)
        print(f"Generated: {dockerinfo_file}")

        # Generate generation_metadata.json
        metadata = self.generate_metadata(services)
        metadata_file = self.output_dir / 'generation_metadata.json'
        with open(metadata_file, 'w') as f:
            json.dump(metadata, f, indent=4)
        print(f"Generated: {metadata_file}")

        # Copy proto files
        self.copy_proto_files(services)

        print(f"\nSuccessfully generated onboarding export with {len(services)} services")
        return True


def main():
    parser = argparse.ArgumentParser(description='Generate AI-Effect onboarding export from use case')
    parser.add_argument('use_case_dir', help='Path to use case directory containing services/ and connections.json')
    parser.add_argument('output_dir', help='Output directory for onboarding export')
    parser.add_argument('--overwrite', action='store_true', help='Overwrite existing output directory')
    parser.add_argument(
        '--zip', nargs='?', const='__default__', default=None,
        metavar='PATH',
        help='Also create a ZIP ready for portal import. With no value, writes '
             '<output_dir>.zip; pass a path to choose the location.',
    )

    args = parser.parse_args()

    if not Path(args.use_case_dir).exists():
        print(f"Error: Use case directory {args.use_case_dir} not found")
        return 1

    if Path(args.output_dir).exists() and not args.overwrite:
        print(f"Error: Output directory {args.output_dir} already exists. Use --overwrite to replace it.")
        return 1

    generator = OnboardingExportGenerator(args.use_case_dir, args.output_dir)
    try:
        generator.generate_export()

        zip_path = None
        if args.zip is not None:
            if args.zip == '__default__':
                zip_path = Path(args.output_dir).with_suffix('.zip')
            else:
                zip_path = Path(args.zip)
            generator.create_zip(zip_path)

        print("\n" + "="*60)
        print("ONBOARDING EXPORT GENERATED")
        print("="*60)
        print(f"Location: {args.output_dir}")
        print("\nFiles created:")
        print("- blueprint.json")
        print("- dockerinfo.json")
        print("- generation_metadata.json")
        print("- microservice/*.proto")
        if zip_path is not None:
            print(f"\nPortal-ready ZIP: {zip_path}")

        return 0
    except Exception as e:
        print(f"Error generating onboarding export: {e}")
        return 1


if __name__ == "__main__":
    exit(main())
