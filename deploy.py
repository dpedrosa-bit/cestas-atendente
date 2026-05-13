"""
deploy.py - Deploy seguro para Cestas Atendente

Uso:
    python deploy.py --check    # so valida (tamanho + sintaxe), sem commit/push
    python deploy.py            # valida, commita e pusha para Railway

IMPORTANTE: Sempre gere os arquivos completos na conversa antes de rodar
(regra de ouro nº 1 — nunca usar patches auto-correctivos).

Mesmo padrão dos projetos cestas-company e cestas-routes.
Railway monitora a branch `main` no GitHub; commits vão para `master` local.

v2 (2026-05-13): chamadas git agora usam lista de args (sem shell=True)
para evitar problemas com aspas/caracteres especiais na mensagem de commit.
Mostra stdout/stderr do git para facilitar diagnostico em caso de erro.
"""
import subprocess
import sys
import ast

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


def run(cmd, *, use_shell=False):
    """Executa comando e retorna (stdout, stderr, returncode).
    Quando cmd eh lista (preferido), nao usa shell — evita problemas de
    quoting com aspas/espacos/caracteres especiais."""
    if isinstance(cmd, list):
        r = subprocess.run(cmd, capture_output=True, text=True)
    else:
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


def sanitize_commit_msg(raw):
    """Remove caracteres problematicos da mensagem do commit.
    - Tira espacos extras
    - Remove comentarios git (linhas que comecam com #) — usuario as vezes cola
      bloco com comentarios markdown
    - Substitui aspas duplas internas por aspas simples (visual)
    - Limita a 200 chars
    """
    if not raw:
        return ''
    # Remove linhas de comentario tipo "# blah"
    lines = [l.strip() for l in raw.split('\n')]
    lines = [l for l in lines if l and not l.startswith('#')]
    cleaned = ' '.join(lines).strip()
    # Se sobrou nada apos limpar (usuario digitou so um comentario), aborta
    if not cleaned:
        return ''
    # Substitui aspas duplas por simples — evita confusao no shell e fica legivel
    cleaned = cleaned.replace('"', "'")
    return cleaned[:200]


def main():
    check_only = '--check' in sys.argv

    if not validate():
        sys.exit(1)

    if check_only:
        print('\nOK - validacao passou (modo --check, sem commit/push)')
        return

    print('\nOK - pronto para deploy')

    raw_msg = input('\nMensagem do commit: ')
    msg = sanitize_commit_msg(raw_msg)
    if not msg:
        msg = 'chore: deploy'
        print(f'  (mensagem vazia ou so comentarios — usando default: "{msg}")')
    else:
        print(f'  Mensagem final: "{msg}"')

    # 1. git add
    print('\n[1/3] git add...')
    out, err, code = run(['git', 'add'] + TRACKED_FILES)
    if code != 0:
        print(f'ERRO git add (exit {code})')
        if out: print(f'  stdout: {out}')
        if err: print(f'  stderr: {err}')
        sys.exit(1)

    # 2. git commit
    print('[2/3] git commit...')
    out, err, code = run(['git', 'commit', '-m', msg])
    combined = (out + ' ' + err).lower()
    if code != 0:
        if 'nothing to commit' in combined:
            resp = input('Sem mudancas a commitar. Push mesmo assim (s/N)? ').strip().lower()
            if resp != 's':
                print('  Abortando.')
                sys.exit(0)
        else:
            print(f'ERRO git commit (exit {code})')
            if out: print(f'  stdout: {out}')
            if err: print(f'  stderr: {err}')
            sys.exit(1)
    else:
        # Mostra resumo do commit
        if out:
            for line in out.split('\n')[:3]:
                print(f'  {line}')

    # 3. git push
    print('[3/3] git push origin master:main...')
    out, err, code = run(['git', 'push', 'origin', 'master:main'])
    if code != 0:
        print(f'ERRO git push (exit {code})')
        if out: print(f'  stdout: {out}')
        if err: print(f'  stderr: {err}')
        sys.exit(1)

    # Mostra resumo do push (geralmente em stderr no git)
    push_summary = err or out
    if push_summary:
        for line in push_summary.split('\n')[:4]:
            print(f'  {line}')

    print('\nDeploy concluido!')
    print('https://cestas-atendente-production.up.railway.app')
    print('https://web-production-0c82e.up.railway.app   (URL Railway gerado)')


if __name__ == '__main__':
    main()
