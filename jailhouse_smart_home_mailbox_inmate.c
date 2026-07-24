/*
 * Smart-home Jailhouse CPU3 mailbox inmate.
 *
 * Linux writes commands into the ROOTSHARED memory window at 0xb1001000.
 * The inmate polls the mailbox and writes an acknowledgement from CPU3.
 */
#include <inmate.h>

#define MAILBOX_BASE            0xb1001000UL
#define MAILBOX_SIZE            0x1000UL
#define MAILBOX_MAGIC           0x53485254U /* SHRT */
#define MAILBOX_VERSION         1U

#define MAILBOX_FLAG_REQUEST    1U
#define MAILBOX_FLAG_ACK        2U

#define PAYLOAD_OFFSET          64U
#define PAYLOAD_SIZE            3072U
#define ACK_OFFSET              (PAYLOAD_OFFSET + PAYLOAD_SIZE)
#define ACK_SIZE                512U

static volatile u32 *mailbox = (volatile u32 *)MAILBOX_BASE;
static volatile char *payload = (volatile char *)(MAILBOX_BASE + PAYLOAD_OFFSET);
static volatile char *ack = (volatile char *)(MAILBOX_BASE + ACK_OFFSET);

static void ack_copy(const char *text)
{
        unsigned int i;

        for (i = 0; i < ACK_SIZE - 1 && text[i]; i++)
                ack[i] = text[i];
        ack[i] = 0;
        mailbox[7] = i;
}

static void process_command(void)
{
        u32 seq = mailbox[2];
        u32 command = mailbox[3];
        u32 length = mailbox[4];
        unsigned int first_payload_byte = (unsigned char)payload[0];

        if (length > PAYLOAD_SIZE)
                length = PAYLOAD_SIZE;

        printk("smart-home mailbox: seq=%u command=%u payload_len=%u first=%u\n",
               seq, command, length, first_payload_byte);

        /*
         * This is the realtime handoff point. Keep the first inmate small:
         * it proves CPU3 receives business commands and can later grow GPIO,
         * sensor sampling or deterministic IR timing handlers here.
         */
        switch (command) {
        case 1:
                ack_copy("cpu3 pong");
                break;
        case 10:
                ack_copy("cpu3 accepted ac_command");
                break;
        case 20:
                ack_copy("cpu3 accepted light_command");
                break;
        case 30:
                ack_copy("cpu3 accepted sensor_sample");
                break;
        default:
                ack_copy("cpu3 accepted command");
                break;
        }

        mailbox[0] = MAILBOX_MAGIC;
        mailbox[1] = MAILBOX_VERSION;
        mailbox[5] = 0;
        mailbox[6] = MAILBOX_FLAG_ACK;
}

void inmate_main(void)
{
        u32 last_seq = 0;

        map_range((void *)MAILBOX_BASE, MAILBOX_SIZE, MAP_UNCACHED);

        printk("smart-home mailbox inmate ready at 0x%lx\n", MAILBOX_BASE);
        mailbox[0] = MAILBOX_MAGIC;
        mailbox[1] = MAILBOX_VERSION;
        mailbox[5] = 0;
        mailbox[6] = MAILBOX_FLAG_ACK;
        ack_copy("cpu3 mailbox ready");

        while (1) {
                if (mailbox[0] == MAILBOX_MAGIC &&
                    mailbox[1] == MAILBOX_VERSION &&
                    mailbox[6] == MAILBOX_FLAG_REQUEST &&
                    mailbox[2] != last_seq) {
                        last_seq = mailbox[2];
                        process_command();
                }
                asm volatile("yield" ::: "memory");
        }
}
