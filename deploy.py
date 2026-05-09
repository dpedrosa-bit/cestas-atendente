"""
deploy.py - Deploy seguro para Cestas Atendente

Uso:
    python deploy.py --check    # so valida (tamanho + sintaxe), sem commit/push
    python deploy.py            # valida, commita e pusha para Railway

IMPORTANTE: Sempre gere os arquivos completos na conversa antes de rodar
(regra de ouro nº 1 — nunca usar patches auto-correctivos).

Mesmo padrão dos projetos cestas-company e cestas-routes.
Railway monitora a branch `main` no GitHub; commits vão para `master` local.
"""
import subprocess, sys, ast

# Arquivos que vao para o deploy (Railway precisa de todos juntos)
TRACKED_FILES = [
    'app.py',
    'models.py',
    'zapi_adapter.py',
    'anthropic_adapter.py',
    'tools.py',
    'shopify_client.py',
    'requirements.txt',
    'Dockerfile',
    'Procfile',
    'nixpacks.toml',
    'railway.toml',
]

# Tamanho minimo para detectar versoes antigas/truncadas
MIN_CHARS = {
    'app.py': 2000,
}


def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.stdout.strip(), r.stderr.strip(), r.returncode


def validate():
    """Valida tamanho e sintaxe Python dos arquivos do deploy."""
    ok = True
    for f in TRACKED_FILES:
        try:
            with open(f, 'r', encoding='utf-8') as fp:
                content = fp.read()
        except FileNotFoundError:
            print(f'ERRO: {f} nao encontrado')
            ok = False
            continue

        chars = len(content)
        print(f'{f}: {chars:,} chars')

        min_size = MIN_CHARS.get(f)
        if min_size and chars < min_size:
            print(f'  ERRO: {f} parece versao antiga ({chars} < {min_size} chars).')
            print('  Gere o arquivo completo na conversa e tente novamente.')
            ok = False
            continue

        if f.endswith('.py'):
            try:
                ast.parse(content)
                print('  Sintaxe: OK')
            except SyntaxError as e:
                print(f'  ERRO sintaxe linha {e.lineno}: {e.msg}')
                ok = False
    return ok


def main():
    check_only = '--check' in sys.argv

    if not validate():
        sys.exit(1)

    if check_only:
        print('\nOK - validacao passou (modo --check, sem commit/push)')
        return

    print('\nOK - pronto para deploy')

    msg = input('\nMensagem do commit: ').strip() or 'chore: deploy'

    run(f'git add {" ".join(TRACKED_FILES)}')
    out, _, _ = run(f'git commit -m "{msg}"')
    if 'nothing to commit' in out:
        if input('Sem mudancas. Push mesmo assim? (s/N): ').lower() != 's':
            sys.exit(0)

    _, err, code = run('git push origin master:main')
    if code == 0:
        print('Deploy concluido!')
        print('https://cestas-atendente-production.up.railway.app')
    else:
        print(f'Erro no push: {err}')


if __name__ == '__main__':
    main()
