import getpass

import bcrypt


def main() -> None:
    senha = getpass.getpass("Senha: ")
    confirmacao = getpass.getpass("Confirme a senha: ")

    if senha != confirmacao:
        raise SystemExit("As senhas nao conferem.")

    senha_hash = bcrypt.hashpw(senha.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    print(senha_hash)


if __name__ == "__main__":
    main()
