#set this to the name of the keystore file containing the private key for this node.
#Make sure that keystore file is in the keystore directory. That directory should be in the same location as this file.
KEYSTORE_FILENAME_TO_USE = 'instance_1'

WEBSOCKET_USE_SSL = False
# The absolute path to your PEM format certificate file.
WEBSOCKET_SSL_CERT_FILE_PATH = '/helios/certs/bootnode_heliosprotocol_io.crt'
# The absolute path to your certificate keyfile.
WEBSOCKET_SSL_KEY_FILE_PATH = '/helios/certs/bootnode_heliosprotocol_io.key'