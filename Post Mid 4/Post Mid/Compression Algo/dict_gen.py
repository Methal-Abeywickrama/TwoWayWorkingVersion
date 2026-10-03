import os
import site
import sys

# Dynamically resolve site-packages paths for zstandard import
possible_paths = site.getusersitepackages()
if isinstance(possible_paths, str):
    possible_paths = [possible_paths]

for path in possible_paths + site.getsitepackages():
    if os.path.exists(path) and path not in sys.path:
        sys.path.insert(0, path)

import zstandard as zstd


def create_chat_dictionary(
    output_filename="chat_dictionary.zdict", dict_size=32768
):
    """Generates and saves a Zstandard compression dictionary optimized for short chat messages."""
    # 1. ASCII-only sample dataset representing chat message payloads
    sample_messages = [
        b'{"sender":"user_101","recipient":"user_202","type":"text","content":"Hello, how are you?"}',
        b'{"sender":"user_303","recipient":"user_101","type":"text","content":"I am doing well, thanks!"}',
        b'{"sender":"user_101","recipient":"user_202","type":"text","content":"Sounds good, lets do it."}',
        b'{"sender":"user_202","recipient":"user_101","type":"text","content":"Are we still meeting at 5 PM today?"}',
        b'{"sender":"user_404","recipient":"user_101","type":"text","content":"Yes, see you at the cafe."}',
        b'{"sender":"user_101","recipient":"user_303","type":"image_ack","content":"Received photo"}',
        b'{"sender":"user_202","recipient":"user_101","type":"typing","content":"true"}',
        b'{"sender":"user_101","recipient":"user_202","type":"presence","status":"online"}',
        b'{"sender":"user_505","recipient":"user_101","type":"text","content":"Can you send me the file?"}',
        b'{"sender":"user_101","recipient":"user_505","type":"text","content":"Sure, sending it right now."}',
    ]

    # Multiply samples to provide sufficient training data
    training_samples = sample_messages * 20

    print(f"Training Zstandard dictionary on {len(training_samples)} samples...")

    # 2. Train the dictionary
    dictionary_data = zstd.train_dictionary(
        dict_size=dict_size, samples=training_samples
    )

    # 3. Save to current script directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_path = os.path.join(script_dir, output_filename)

    with open(output_path, "wb") as f:
        f.write(dictionary_data.as_bytes())

    print(
        f"Successfully generated '{output_filename}' ({len(dictionary_data.as_bytes())} bytes) at:"
    )
    print(f"  -> {output_path}")


if __name__ == "__main__":
    create_chat_dictionary()
