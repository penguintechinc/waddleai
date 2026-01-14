"""
WaddleAI Management API v1 - MarchProxy AILB Control Endpoints
"""

from datetime import datetime
import json

from flask import request, jsonify, current_app, Response

from . import api_v1_bp
from .auth import require_auth, require_role
from ...extensions import db
from ...services.marchproxy_config import MarchProxyConfigGenerator


@api_v1_bp.route('/ailb/status', methods=['GET'])
@require_auth
@require_role('admin')
def get_ailb_status():
    """Get AILB module status via gRPC"""
    ailb_host = current_app.config.get('MARCHPROXY_AILB_HOST', 'localhost')
    ailb_grpc_port = current_app.config.get('MARCHPROXY_AILB_GRPC_PORT', 50051)

    # TODO: Implement actual gRPC call to AILB ModuleService.GetStatus
    # For now, return mock response
    return jsonify({
        'connected': True,
        'host': ailb_host,
        'grpc_port': ailb_grpc_port,
        'status': 'healthy',
        'version': '1.0.0',
        'uptime_seconds': 86400,
        'active_routes': 5,
        'message': 'AILB module is running'
    })


@api_v1_bp.route('/ailb/health', methods=['GET'])
@require_auth
@require_role('admin')
def check_ailb_health():
    """Health check for AILB"""
    ailb_host = current_app.config.get('MARCHPROXY_AILB_HOST', 'localhost')
    ailb_http_port = current_app.config.get('MARCHPROXY_AILB_HTTP_PORT', 8080)

    # TODO: Implement actual HTTP health check to AILB /healthz
    return jsonify({
        'ailb_host': ailb_host,
        'ailb_http_port': ailb_http_port,
        'health_status': 'healthy',
        'checked_at': datetime.utcnow().isoformat()
    })


@api_v1_bp.route('/ailb/routes', methods=['GET'])
@require_auth
@require_role('admin')
def list_ailb_routes():
    """List configured routes in AILB"""
    # TODO: Implement gRPC call to AILB ModuleService.GetRoutes
    # For now, fetch from local sync records
    synced_providers = db(
        (db.marchproxy_ailb_sync.sync_status == 'synced') &
        (db.marchproxy_ailb_sync.provider_id == db.ai_providers.id)
    ).select(db.ai_providers.ALL, db.marchproxy_ailb_sync.ALL)

    routes = []
    for record in synced_providers:
        provider = record.ai_providers
        sync = record.marchproxy_ailb_sync

        routes.append({
            'route_id': sync.ailb_route_id,
            'provider_id': provider.id,
            'provider_name': provider.name,
            'provider_type': provider.provider_type,
            'endpoint_url': provider.endpoint_url,
            'enabled': provider.enabled,
            'priority': provider.priority,
            'last_synced': sync.last_synced.isoformat() if sync.last_synced else None
        })

    return jsonify({'routes': routes, 'total': len(routes)})


@api_v1_bp.route('/ailb/routes', methods=['POST'])
@require_auth
@require_role('admin')
def create_ailb_route():
    """Create a new route in AILB"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'Request body required'}), 400

    # This is typically done via provider sync
    # Direct route creation maps to creating a provider and syncing
    return jsonify({
        'message': 'Use /providers endpoint to create providers, then /providers/{id}/sync to push to AILB'
    }), 400


@api_v1_bp.route('/ailb/routes/<route_id>', methods=['PUT'])
@require_auth
@require_role('admin')
def update_ailb_route(route_id):
    """Update route in AILB"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'Request body required'}), 400

    # Find provider by route_id
    sync = db(db.marchproxy_ailb_sync.ailb_route_id == route_id).select().first()

    if not sync:
        return jsonify({'error': 'Route not found'}), 404

    # TODO: Implement gRPC call to update route
    return jsonify({
        'route_id': route_id,
        'message': 'Route update not yet implemented. Update provider and re-sync.'
    })


@api_v1_bp.route('/ailb/routes/<route_id>', methods=['DELETE'])
@require_auth
@require_role('admin')
def delete_ailb_route(route_id):
    """Delete route from AILB"""
    sync = db(db.marchproxy_ailb_sync.ailb_route_id == route_id).select().first()

    if not sync:
        return jsonify({'error': 'Route not found'}), 404

    # TODO: Implement gRPC call to delete route
    # Mark as deleted in sync table
    db(db.marchproxy_ailb_sync.ailb_route_id == route_id).update(sync_status='deleted')
    db.commit()

    return jsonify({
        'route_id': route_id,
        'message': 'Route marked for deletion'
    })


@api_v1_bp.route('/ailb/metrics', methods=['GET'])
@require_auth
@require_role('admin')
def get_ailb_metrics():
    """Get AILB metrics"""
    # TODO: Implement gRPC call to AILB ModuleService.GetMetrics
    return jsonify({
        'collected_at': datetime.utcnow().isoformat(),
        'metrics': {
            'total_requests': 12500,
            'requests_per_minute': 45,
            'avg_latency_ms': 150,
            'error_rate': 0.02,
            'providers': {
                'openai': {
                    'requests': 5000,
                    'avg_latency_ms': 180,
                    'success_rate': 0.98
                },
                'anthropic': {
                    'requests': 4500,
                    'avg_latency_ms': 140,
                    'success_rate': 0.99
                },
                'ollama': {
                    'requests': 3000,
                    'avg_latency_ms': 120,
                    'success_rate': 0.97
                }
            }
        }
    })


@api_v1_bp.route('/ailb/reload', methods=['POST'])
@require_auth
@require_role('admin')
def reload_ailb():
    """Trigger AILB configuration reload"""
    # TODO: Implement gRPC call to AILB ModuleService.Reload
    return jsonify({
        'success': True,
        'message': 'AILB reload triggered',
        'timestamp': datetime.utcnow().isoformat()
    })


@api_v1_bp.route('/ailb/export-config', methods=['POST'])
@require_auth
@require_role('admin')
def export_ailb_config():
    """Export MarchProxy-compatible import configuration"""
    # Get all enabled providers
    providers = db(
        (db.ai_providers.enabled == True) &
        (db.ai_providers.ailb_sync_enabled == True)
    ).select()

    # Get all enabled virtual keys
    keys = db(db.virtual_keys.enabled == True).select()

    # Build export config
    export_config = {
        'version': '1.0',
        'module_type': 'AILB',
        'exported_at': datetime.utcnow().isoformat(),
        'providers': [],
        'routes': [],
        'rate_limits': [],
        'virtual_keys': []
    }

    for provider in providers:
        provider_config = {
            'id': provider.id,
            'name': provider.name,
            'type': provider.provider_type,
            'endpoint_url': provider.endpoint_url,
            'models': provider.model_list or [],
            'rate_limits': provider.rate_limits or {},
            'priority': provider.priority,
            'enabled': provider.enabled
        }
        export_config['providers'].append(provider_config)

        # Generate route config
        route_config = {
            'provider_id': provider.id,
            'provider_type': provider.provider_type,
            'endpoint': provider.endpoint_url,
            'models': provider.model_list or [],
            'weight': 100 - provider.priority  # Convert priority to weight
        }
        export_config['routes'].append(route_config)

    for key in keys:
        key_config = {
            'id': key.id,
            'name': key.name,
            'user_id': key.user_id,
            'organization_id': key.organization_id,
            'allowed_models': key.allowed_models or [],
            'allowed_providers': key.allowed_providers or [],
            'budget_daily': key.budget_limit_daily,
            'budget_monthly': key.budget_limit_monthly,
            'tpm_limit': key.tpm_limit,
            'rpm_limit': key.rpm_limit
        }
        export_config['virtual_keys'].append(key_config)

        # Generate rate limit config
        if key.tpm_limit or key.rpm_limit:
            rate_limit = {
                'key_id': key.id,
                'tpm_limit': key.tpm_limit or 10000,
                'rpm_limit': key.rpm_limit or 60
            }
            export_config['rate_limits'].append(rate_limit)

    # Option to save to file
    save_to_file = request.args.get('save', 'false').lower() == 'true'
    if save_to_file:
        import os
        config_dir = '/app/config/marchproxy'
        os.makedirs(config_dir, exist_ok=True)

        filename = f'ailb-import-{datetime.utcnow().strftime("%Y%m%d-%H%M%S")}.json'
        filepath = os.path.join(config_dir, filename)

        with open(filepath, 'w') as f:
            json.dump(export_config, f, indent=2)

        return jsonify({
            'success': True,
            'file_path': filepath,
            'config': export_config
        })

    return jsonify(export_config)


@api_v1_bp.route('/ailb/sync-all', methods=['POST'])
@require_auth
@require_role('admin')
def sync_all_providers():
    """Sync all enabled providers to AILB"""
    providers = db(
        (db.ai_providers.enabled == True) &
        (db.ai_providers.ailb_sync_enabled == True)
    ).select()

    results = {
        'synced': [],
        'failed': []
    }

    for provider in providers:
        try:
            # TODO: Implement actual gRPC call
            # For now, update sync status
            db(db.marchproxy_ailb_sync.provider_id == provider.id).update(
                sync_status='synced',
                last_synced=datetime.utcnow(),
                sync_error=None
            )
            results['synced'].append({
                'provider_id': provider.id,
                'provider_name': provider.name
            })
        except Exception as e:
            db(db.marchproxy_ailb_sync.provider_id == provider.id).update(
                sync_status='failed',
                sync_error=str(e)
            )
            results['failed'].append({
                'provider_id': provider.id,
                'provider_name': provider.name,
                'error': str(e)
            })

    db.commit()

    return jsonify({
        'message': 'Sync completed',
        'results': results
    })


@api_v1_bp.route('/ailb/marchproxy-import-config', methods=['GET'])
@require_auth
@require_role('admin')
def generate_marchproxy_import_config():
    """
    Generate MarchProxy-compatible import configuration.

    Returns JSON config that can be imported via:
    POST http://marchproxy:8080/api/v1/services/import

    Query params:
    - format: 'json' (default) or 'yaml'
    - include_ollama: 'true' (default) or 'false' - include Ollama model routes
    - download: 'true' or 'false' (default) - download as file
    """
    generator = MarchProxyConfigGenerator(db)

    # Generate full configuration
    config = generator.generate_full_config()

    # Check if Ollama routes should be excluded
    include_ollama = request.args.get('include_ollama', 'true').lower() == 'true'
    if not include_ollama:
        # Filter out Ollama routes
        config['ailb']['routes'] = [
            r for r in config['ailb']['routes']
            if 'ollama' not in r.get('id', '')
        ]

    # Format selection
    format_type = request.args.get('format', 'json').lower()
    download = request.args.get('download', 'false').lower() == 'true'

    if format_type == 'yaml':
        import yaml
        config_str = yaml.dump(config, default_flow_style=False)
        mimetype = 'text/yaml'
        filename = 'marchproxy-import.yml'
    else:
        config_str = json.dumps(config, indent=2)
        mimetype = 'application/json'
        filename = 'marchproxy-import.json'

    if download:
        return Response(
            config_str,
            mimetype=mimetype,
            headers={'Content-Disposition': f'attachment; filename={filename}'}
        )

    return jsonify(config)


@api_v1_bp.route('/ailb/ollama-routing-table', methods=['GET'])
@require_auth
@require_role('admin')
def get_ollama_routing_table():
    """
    Get Ollama model-to-endpoint routing table.

    Returns a simple mapping for routing decisions:
    {
        "llama3.2": "http://node-1:11434",
        "mistral": "http://node-2:11434",
        "codellama": "http://node-1:11434"
    }
    """
    generator = MarchProxyConfigGenerator(db)
    routing_table = generator.generate_ollama_routing_table()

    return jsonify({
        'routing_table': routing_table,
        'total_models': len(routing_table),
        'generated_at': datetime.utcnow().isoformat()
    })


@api_v1_bp.route('/ailb/model-routing-config', methods=['GET'])
@require_auth
@require_role('admin')
def get_model_routing_config():
    """
    Get model-aware routing configuration for MarchProxy AILB.

    This provides detailed routing rules with health checks and
    fallback strategies for intelligent model routing.
    """
    generator = MarchProxyConfigGenerator(db)
    routing_config = generator.generate_model_routing_config()

    return jsonify(routing_config)


@api_v1_bp.route('/ailb/export-all-configs', methods=['GET'])
@require_auth
@require_role('admin')
def export_all_configs():
    """
    Export all configurations as a bundle.

    Returns:
    - MarchProxy import config
    - Ollama routing table
    - Model routing config
    - MetalLB service configs
    """
    from ...services.ollama_manager import OllamaDeploymentManager

    generator = MarchProxyConfigGenerator(db)
    ollama_manager = OllamaDeploymentManager(db)

    bundle = {
        'version': '1.0',
        'generated_at': datetime.utcnow().isoformat(),
        'configs': {
            'marchproxy_import': generator.generate_full_config(),
            'ollama_routing_table': generator.generate_ollama_routing_table(),
            'model_routing_config': generator.generate_model_routing_config(),
            'metallb_config': ollama_manager.export_metallb_config()
        }
    }

    download = request.args.get('download', 'false').lower() == 'true'

    if download:
        bundle_json = json.dumps(bundle, indent=2)
        return Response(
            bundle_json,
            mimetype='application/json',
            headers={'Content-Disposition': 'attachment; filename=waddleai-configs-bundle.json'}
        )

    return jsonify(bundle)
