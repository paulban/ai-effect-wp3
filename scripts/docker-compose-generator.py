#!/usr/bin/env python3
"""
Docker Compose Generator for AI-Effect Onboarding Package

This script reads blueprint.json and dockerinfo.json to generate a docker-compose.yml 
file for deploying the AI-Effect pipeline services.
"""

import json
import yaml
import argparse
from pathlib import Path


class DockerComposeGenerator:
    def __init__(self, base_port=50051):
        self.base_port = base_port
        self.services = {}
        self.networks = {"ai-effect-services": {"name": "ai-effect-services", "external": True}}
        self.volumes = {}
    
    def load_blueprint(self, blueprint_file):
        """Load blueprint.json and extract service information"""
        with open(blueprint_file, 'r') as f:
            blueprint = json.load(f)
        
        print(f"Loading blueprint: {blueprint.get('name', 'Unknown Pipeline')}")
        return blueprint
    
    def load_dockerinfo(self, dockerinfo_file):
        """Load dockerinfo.json and extract port mappings"""
        if not dockerinfo_file.exists():
            return {}

        try:
            with open(dockerinfo_file, 'r') as f:
                dockerinfo = json.load(f)

            port_mapping = {}

            if 'docker_info_list' in dockerinfo:
                for item in dockerinfo['docker_info_list']:
                    container_name = item.get('container_name')
                    port = item.get('port')
                    if container_name and port:
                        port_mapping[container_name] = int(port)

            return port_mapping

        except Exception as e:
            print(f"Warning: Could not load dockerinfo.json: {e}")
            return {}

    def generate_compose_service(self, node, external_port):
        """Generate docker-compose service configuration for a node"""
        container_name = node['container_name']
        image_name = node.get('image')

        if not image_name:
            print(f"Warning: No image found in blueprint for container {container_name}")
            return None

        # Use standard internal port for all services - they should read GRPC_PORT env var
        internal_port = 50051

        service = {
            'image': image_name,
            'container_name': container_name,
            'ports': [f"{external_port}:{internal_port}"],
            'networks': ['ai-effect-services'],
            'volumes': ['./data:/app/data'],
            'environment': {
                'GRPC_PORT': str(internal_port),
                'SERVICE_NAME': container_name
            },
            'restart': 'unless-stopped'
        }

        return service
    
    def extract_dependencies(self, blueprint):
        """Extract service dependencies from blueprint connections"""
        dependencies = {}
        
        for node in blueprint['nodes']:
            container_name = node['container_name']
            depends_on = []
            
            # Find what services this node connects to
            for op_sig in node.get('operation_signature_list', []):
                for connection in op_sig.get('connected_to', []):
                    target_container = connection['container_name']
                    depends_on.append(target_container)
            
            if depends_on:
                dependencies[container_name] = depends_on
        
        return dependencies
    
    def generate_orchestrator_service(self, export_dir, orchestrator_path, all_services):
        """Generate orchestrator service configuration"""

        # Calculate relative path from export_dir to orchestrator using os.path
        import os
        relative_path = os.path.relpath(orchestrator_path, export_dir)

        orchestrator_service = {
            'build': relative_path,
            'container_name': 'orchestrator',
            'entrypoint': ['/bin/sh'],
            'command': ['-c', 'sleep 5 && python orchestrator.py /export'],
            'volumes': [
                '.:/export:ro'  # Mount current directory (export dir) as read-only
            ],
            'networks': ['ai-effect-services'],
            'depends_on': list(all_services.keys()),  # Wait for all services to start
            'restart': 'no'  # Run once and exit
        }

        return orchestrator_service

    def generate_docker_compose(self, blueprint_file, dockerinfo_file, output_file, orchestrator_path=None):
        """Generate complete docker-compose.yml file"""

        # Load blueprint
        blueprint = self.load_blueprint(blueprint_file)

        # Load port mappings from dockerinfo if available
        port_mapping = self.load_dockerinfo(dockerinfo_file)

        # Extract service dependencies
        dependencies = self.extract_dependencies(blueprint)

        # Generate services
        # Auto-assign external ports (dockerinfo has internal ports for orchestrator use)
        external_port = self.base_port
        for node in blueprint['nodes']:
            container_name = node['container_name']

            service = self.generate_compose_service(node, external_port)
            if service:
                self.services[container_name] = service
                external_port += 1

        # Add orchestrator service if path provided
        if orchestrator_path:
            export_dir = Path(output_file).parent.absolute()
            orchestrator_service = self.generate_orchestrator_service(
                export_dir, orchestrator_path, self.services
            )
            self.services['orchestrator'] = orchestrator_service

        # Create complete docker-compose structure
        compose_config = {
            'services': self.services,
            'networks': self.networks
        }

        # Only add volumes section if there are any
        if self.volumes:
            compose_config['volumes'] = self.volumes

        # Write docker-compose.yml
        with open(output_file, 'w') as f:
            yaml.dump(compose_config, f, default_flow_style=False, indent=2)

        # Create data directory for bind mount
        data_dir = Path(output_file).parent / 'data'
        data_dir.mkdir(exist_ok=True)

        service_count = len(self.services) - (1 if orchestrator_path else 0)
        print(f"Generated docker-compose.yml with {service_count} services" +
              (" + orchestrator" if orchestrator_path else ""))
        print(f"Created data directory: {data_dir}")
        print("Note: Images must be built before deployment (use build_and_tag.sh in use-cases/)")
        return compose_config


def main():
    parser = argparse.ArgumentParser(description='Generate Docker Compose from AI-Effect onboarding package')
    parser.add_argument('onboarding_dir', help='Path to AI-Effect onboarding export directory')
    parser.add_argument('--output', help='Output docker-compose.yml file (default: onboarding_dir/docker-compose.yml)')
    parser.add_argument('--base-port', type=int, default=50051, help='Base port for services')
    parser.add_argument('--orchestrator-path', help='Path to orchestrator directory (includes orchestrator in deployment)')

    args = parser.parse_args()

    # Set up paths
    onboarding_path = Path(args.onboarding_dir)
    blueprint_file = onboarding_path / 'blueprint.json'
    dockerinfo_file = onboarding_path / 'dockerinfo.json'
    output_file = Path(args.output) if args.output else onboarding_path / 'docker-compose.yml'

    # Verify blueprint.json exists
    if not blueprint_file.exists():
        print(f"Error: Blueprint file {blueprint_file} not found")
        return 1

    # Verify orchestrator path if provided
    orchestrator_path = None
    if args.orchestrator_path:
        orchestrator_path = Path(args.orchestrator_path)
        if not orchestrator_path.exists():
            print(f"Error: Orchestrator path {orchestrator_path} not found")
            return 1
        if not (orchestrator_path / 'Dockerfile').exists():
            print(f"Error: Dockerfile not found in {orchestrator_path}")
            return 1

    # Generate docker-compose.yml
    generator = DockerComposeGenerator(base_port=args.base_port)
    try:
        generator.generate_docker_compose(blueprint_file, dockerinfo_file, output_file, orchestrator_path)

        print("\n" + "="*60)
        print("DOCKER COMPOSE USAGE")
        print("="*60)
        print(f"Generated: {output_file}")
        print(f"\nTo deploy:")
        print(f"1. Ensure images are built (run build_and_tag.sh in use-cases directory)")
        if args.orchestrator_path:
            print(f"2. Start all services: cd {onboarding_path} && docker compose up")
            print(f"   (orchestrator will run workflow and exit)")
            print(f"3. View logs: docker compose logs orchestrator")
            print(f"4. Stop services: docker compose down")
        else:
            print(f"2. Start services: cd {onboarding_path} && docker compose up -d")
            print(f"3. Run orchestrator manually from host")
            print(f"4. View logs: docker compose logs -f")
            print(f"5. Stop services: docker compose down")

        return 0
    except Exception as e:
        print(f"Error generating docker-compose.yml: {e}")
        return 1


if __name__ == "__main__":
    exit(main())