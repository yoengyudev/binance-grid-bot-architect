"""Run only the isolated, public-data SOL paper strategy on loopback."""
import argparse
from pathlib import Path
import secrets

import uvicorn
from trend_following.service import Config, configure_logging, create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',default='trend-paper.env')
    parser.add_argument('--init-config',action='store_true',help='Create a new local configuration with random credentials')
    parser.add_argument('--check-config',action='store_true')
    parser.add_argument('--port',type=int,default=8010)
    args = parser.parse_args()
    filename = Path(args.env_file).resolve()
    if args.init_config:
        example = Path(__file__).with_name('trend-paper.env.example').read_text()
        example = example.replace('CHANGE_TO_A_PRIVATE_PASSWORD',secrets.token_urlsafe(20))
        example = example.replace('CHANGE_TO_A_RANDOM_SECRET_OF_32_OR_MORE_CHARACTERS',secrets.token_urlsafe(48))
        with filename.open('x',encoding='utf-8') as target:
            target.write(example)
        filename.chmod(0o600)
        print('Created private configuration: '+str(filename))
        return
    config = Config.load(filename)
    if args.check_config:
        print('PAPER only; SOLUSDT; dedicated database: '+str(config.database))
        return
    configure_logging(config)
    uvicorn.run(create_app(config),host='127.0.0.1',port=args.port,access_log=False)


if __name__=='__main__':
    main()
