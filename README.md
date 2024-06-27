### Overview
Montytest is a fishtest derivative: https://github.com/official-stockfish/fishtest

Montytest is a distributed task queue for testing chess engines. The main instance
for testing the chess engine [Monty](https://github.com/official-monty/Monty) is at this web page https://montychess.org

Developers submit patches with new ideas and improvements, CPU contributors install a montytest worker on their computers to play some chess games in the background to help the developers testing the patches.

The montytest worker:
- Automatically connects to the server to download a chess opening book, the [cutechess-cli](https://github.com/cutechess/cutechess) chess game manager and the chess engine sources (both for the current Monty and for the patch with the new idea). The sources will be compiled according to the type of the worker platform.
- Starts a batch of games using cutechess-cli.
- Uploads the games results on the server.

The montytest server:
- Manages the queue of the tests with customizable priorities.
- Computes several probabilistic values from the game results sent by the workers.
- Updates and publishes the results of ongoing tests.
- Knows how to stop tests when they are statistically significant and publishes the final tests results.

To get more information, such as the worker/server install and configuration instructions, visit the [montytest Wiki](https://github.com/official-monty/montytest/wiki).
