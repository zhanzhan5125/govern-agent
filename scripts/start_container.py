"""Credential-free image; operator configuration lives only in the state volume."""
import json
import os
from pathlib import Path
from urllib.parse import quote


def main():
    if os.environ.get('GOVERN_SETTINGS_ALLOW_DOCKER_GATEWAY')=='1':
        # Trust only the actual default gateway, never an entire RFC1918 subnet.
        import socket
        routes=Path('/proc/net/route').read_text().splitlines()[1:]
        gateway=next((row.split()[2] for row in routes if row.split()[1]=='00000000'),None)
        if gateway:os.environ['GOVERN_SETTINGS_TRUSTED_PEER']=socket.inet_ntoa(bytes.fromhex(gateway)[::-1])
    mode = os.environ.get('GOVERN_STORAGE', 'sqlite')
    if mode == 'postgres':
        user = quote(os.environ['GOVERN_POSTGRES_USER'], safe='')
        password = quote(os.environ['GOVERN_POSTGRES_PASSWORD'], safe='')
        database = quote(os.environ['GOVERN_POSTGRES_DATABASE'], safe='')
        host = os.environ['GOVERN_POSTGRES_HOST']
        os.environ['EVOAGENT_DATABASE_URL'] = f'postgresql://{user}:{password}@{host}:5432/{database}'
    elif mode == 'sqlite':
        os.environ['EVOAGENT_DATABASE_URL'] = ''
    else:
        raise ValueError('GOVERN_STORAGE must be sqlite or postgres')
    location = Path(os.environ['GOVERN_CONFIG'])
    location.parent.mkdir(parents=True, exist_ok=True)
    if not location.exists():
        with location.open('x', encoding='utf-8') as target:
            json.dump({'auto_evolution': {'enabled': False}, 'execution_profiles': {}}, target)
        location.chmod(0o600)
    # Match the local launcher: initialize service clients from saved config,
    # without placing provider credentials into the image or Compose file.
    config = json.loads(location.read_text(encoding='utf-8'))
    model = config.get('model', {})
    if all(model.get(k) for k in ('base_url', 'api_key', 'model')):
        os.environ.update(EVOAGENT_LLM_PROVIDER='custom', EVOAGENT_LLM_BASE_URL=model['base_url'],
                          EVOAGENT_LLM_API_KEY=model['api_key'], EVOAGENT_LLM_MODEL=model['model'])
    from govern.api.api import run
    run()


if __name__ == '__main__':
    main()
