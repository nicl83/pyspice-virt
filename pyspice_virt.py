from ast import Constant
from email import header
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_OAEP
import asyncio
import logging
import struct
# https://www.spice-space.org/spice-protocol.html

# TODO: better logging config
logging.basicConfig(
    level=logging.DEBUG
)

# Define constants for the SPICE protocol

SPICE_MAGIC = int.from_bytes(b'REDQ', byteorder="little")

SPICE_VERSION_MAJOR = 2
SPICE_VERSION_MINOR = 2

SPICE_CHANNEL_MAIN      = 1
SPICE_CHANNEL_DISPLAY   = 2
SPICE_CHANNEL_INPUTS    = 3
SPICE_CHANNEL_CURSOR    = 4
SPICE_CHANNEL_PLAYBACK  = 5
SPICE_CHANNEL_RECORD    = 6
SPICE_CHANNEL_TUNNEL    = 7 # obsolete
SPICE_CHANNEL_SMARTCARD = 8
SPICE_CHANNEL_USBREDIR  = 9
SPICE_CHANNEL_PORT      = 10
SPICE_CHANNEL_WEBDAV    = 11

SPICE_LINK_ERR_DICT = {
    0: "SPICE_LINK_ERR_OK",                   
    1: "SPICE_LINK_ERR_ERROR",                
    2: "SPICE_LINK_ERR_INVALID_MAGIC",        
    3: "SPICE_LINK_ERR_INVALID_DATA",         
    4: "SPICE_LINK_ERR_VERSION_MISMATCH",     
    5: "SPICE_LINK_ERR_NEED_SECURED",         
    6: "SPICE_LINK_ERR_NEED_UNSECURED",       
    7: "SPICE_LINK_ERR_PERMISSION_DENIED",    
    8: "SPICE_LINK_ERR_BAD_CONNECTION_ID",    
    9: "SPICE_LINK_ERR_CHANNEL_NOT_AVAILABLE"
}

SPICE_LINK_ERR_OK                       = 0
SPICE_LINK_ERR_ERROR                    = 1
SPICE_LINK_ERR_INVALID_MAGIC            = 2
SPICE_LINK_ERR_INVALID_DATA             = 3
SPICE_LINK_ERR_VERSION_MISMATCH         = 4
SPICE_LINK_ERR_NEED_SECURED             = 5
SPICE_LINK_ERR_NEED_UNSECURED           = 6
SPICE_LINK_ERR_PERMISSION_DENIED        = 7
SPICE_LINK_ERR_BAD_CONNECTION_ID        = 8
SPICE_LINK_ERR_CHANNEL_NOT_AVAILABLE    = 9

SPICE_WARN_GENERAL                      = 0
SPICE_INFO_GENERAL                      = 0

# This is redundant in Python, as buffer size is a non-issue...?
# Never the less, keep it in, just in case
SPICE_TICKET_PUBKEY_BYTES = 162

SPICE_LINK_MESSAGE_STRUCT = "<5I2B3I" # 4 UINT32s, 2 UINT8s, 3 UINT32s
                                     # see struct docs, i can't help

SPICE_LINK_MESSAGE_DUMMY_STRUCT = "<4I" # used for size calculation

SPICE_LINK_REPLY_STRUCT   = f"<5I {SPICE_TICKET_PUBKEY_BYTES}s 3I"
SPICE_DATA_HEADER_STRUCT  = "<QH2I"

# HACK
# This struct is based on observed data, not the spec.
# If this breaks, yell at me, and also at the QEMU devs.
SPICE_DATA_HEADER_NOSERIAL_STRUCT = "<HI"


# What can PySpice_VIRT actually do?
SPICE_CLIENT_CAPABILITIES = 0x9 # HACK, see below
SPICE_CHANNEL_CAPABILITIES = {
    SPICE_CHANNEL_MAIN: 0xF # Temporary HACK to test protocol
                            # stolen from wireshark capture of virt_viewer 
}

# End constant definition

def _create_spice_ticket(keydata, password):
    """
    Internal function to encrypt a password with the server's provided RSA key.
    """
    password = bytes(password, encoding='utf-8')
    rsa_key = RSA.import_key(keydata)
    rsa_cipher = PKCS1_OAEP.new(rsa_key)
    ticket = rsa_cipher.encrypt(password)
    return ticket

def _get_struct_size(struct_input):
    """
    Internal function to get the size of a packed struct
    """
    return struct.Struct(struct_input).size

class SpiceClient:
    """
    A class implementing a SPICE client. This class does NOT implement a GUI -
    this is left as an exercise for the reader.
    """

    def __init__(self, hostname, port):
        """
        Initialise the SPICE client.
        """
        self.hostname = hostname
        self.password = None # set later by connect_init
        self.port = port
        self.channels = {}
    
    def _connect_init(self, password):
        """
        Connect to the server's main channel and fetch info.
        WARNING - The password WILL be stored as a property of this instance!
        TODO: find better way of handling this
        """

        self.password = password
    
    async def join_channel(self, channel_type):
        self.channels[channel_type] = SpiceChannel(
            channel_type,
            self.password,
            self.hostname,
            self.port
        )
        await self.channels[channel_type].create_connection()
    
    def end_session(self):
        for channel in self.channels:
            self.channels[channel].cleanup()

 
class SpiceChannel:
    """
    An internal class for a SPICE channel connection.
    Do not manually instantiate, please use SpiceClient.
    """
    def __init__(self, channel_type, password, hostname, port, session_id=0):
        self.channel_type = channel_type
        self.password = password
        self.hostname = hostname
        self.port = port
        self.session_id = session_id
    
    async def create_connection(self):
        """
        Initialise a connection to the server.
        """
        self.reader, self.writer = await asyncio.open_connection(
            self.hostname, self.port
        )
        logging.debug(f"Connection for channel {self.channel_type} opened")
        # ok, here comes the fun part
        # pack a SpiceLinkMessage, send it, and listen to the reply
        # :pray:
        
        # HACK time - assume one Word for capabilities.
        # If we write code that uses more than four bytes for caps.
        # we can come back to this later
        num_common_caps = 1
        num_channel_caps = (
            1 if self.channel_type in SPICE_CHANNEL_CAPABILITIES else 0
        )

        spice_link_message = struct.pack(
            SPICE_LINK_MESSAGE_STRUCT,
            SPICE_MAGIC,
            SPICE_VERSION_MAJOR,
            SPICE_VERSION_MINOR,
            26, # HACK this should be the sze of SpiceLinkMess after this!
            self.session_id,
            self.channel_type,
            0, # HACK connect to only the first channel of this type
            num_common_caps, # words for common capabilities 
            num_channel_caps, # words for channel-specific capabilities
            18 # HACK seems constant but should be defined as such
        ) + bytes([SPICE_CLIENT_CAPABILITIES]).ljust(4,b'\0')\
          + bytes(
              [SPICE_CHANNEL_CAPABILITIES.get(self.channel_type, None)]
            ).ljust(4,b'\0')

        logging.debug(f"Sending SPICE_LINK_MESS for ch. {self.channel_type}...")
        self.writer.write(spice_link_message)
        await self.writer.drain()
        
        # HACK - we're technically disregarding the capabilities of the
        # server here. I'm building this around QEMU 7. This may change in the
        # future. Ideally, I need to write code to correctly fetch the
        # server capabiltiies.
        logging.debug(f"Waiting for server response...")
        spice_link_reply_data = await self.reader.read(
            _get_struct_size(SPICE_LINK_REPLY_STRUCT)
        )
        spice_link_reply = struct.unpack(
            SPICE_LINK_REPLY_STRUCT,
            spice_link_reply_data
        )
        # logging.debug((
        #     "REPLY FROM SERVER:",
        #     spice_link_reply
        # ))

        if spice_link_reply[0] != SPICE_MAGIC:
            logging.warn("Server reply didn't have correct magic!")
        else:
            logging.debug("Server magic OK!!")
        spice_link_error_code = spice_link_reply[4]
        if spice_link_error_code == 0:
            logging.debug("Relax and breathe - server accepted connection")
        else:
            error_name = SPICE_LINK_ERR_DICT.get(spice_link_error_code, "other")
            logging.error(f"Server returned non-0 error: {error_name}")
        
        # Let's finish the handshake.
        logging.debug("Attempting to authenticate with given key...")

        # HACK - this effectively forces SPICE auth!
        # Not a problem rn, but...
        self.writer.write(b'\x01\x00\x00\x00') # SPICE auth magic
        await self.writer.drain()

        given_key = spice_link_reply[5]
        ticket = _create_spice_ticket(given_key, self.password)
        self.writer.write(ticket)
        await self.writer.drain()
        
        auth_result = await self.reader.read(11)
        logging.debug("Abandoning authentication attempt, you are on your own")
        logging.debug(auth_result)
        # if auth_result == 0:
        #     logging.debug("Authentication succesful!")
        #     self.task = asyncio.create_task(self.msg_loop())
        # else:
        #     error_name = SPICE_LINK_ERR_DICT.get(auth_result, auth_result)
        #     logging.error(f"Server returned non-0 error: {error_name}")
        self.writer.close()
    
    def cleanup(self):
        self.writer.close()

    def __str__(self) -> str:
        if self.connected:
            firstpart = "Active SPICE channel, "
        else:
            firstpart = "Dead SPICE channel, "
        return firstpart + f"server {self.hostname} port {self.port}"

    async def msg_loop(self):
        logging.debug(f"Entering message loop for channel {self.channel_type}")
        while True:
            incoming_header = await self.reader.read(6)
            logging.debug(("raw header", incoming_header))
            header_info = struct.unpack(
                SPICE_DATA_HEADER_NOSERIAL_STRUCT,
                incoming_header)
            msg_data = await self.reader.read(header_info[1])
            logging.debug(
                f"Recieved message type {header_info[0]} size {header_info[1]}"
            )
            logging.debug(f"Data: {msg_data}")
