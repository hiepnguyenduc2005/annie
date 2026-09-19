"""Create a bounded-context local model alias without changing Ollama defaults."""
import httpx


def main():
    # Requires the public Qwen model to be installed first (ollama pull).
    with httpx.Client(base_url='http://127.0.0.1:11434',timeout=60,trust_env=False) as client:
        response=client.post('/api/create',json={'model':'annie-qwen3-vl:2b',
            'from':'qwen3-vl:2b-instruct','parameters':{'num_ctx':8192,'temperature':0,'num_predict':256},
            'stream':False})
        response.raise_for_status()
        print('Prepared annie-qwen3-vl:2b with 8192-token context; no cloud inference.')


if __name__=='__main__':main()
