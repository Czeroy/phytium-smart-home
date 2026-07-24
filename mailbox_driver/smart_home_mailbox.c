// SPDX-License-Identifier: GPL-2.0
/* Linux side of the smart-home Jailhouse shared-memory mailbox. */

#include <linux/delay.h>
#include <linux/fs.h>
#include <linux/io.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/uaccess.h>

#define DRIVER_NAME             "smart_home_mailbox"
#define MAILBOX_MAGIC           0x53485254U
#define MAILBOX_VERSION         1U
#define MAILBOX_FLAG_REQUEST    1U
#define MAILBOX_FLAG_ACK        2U

#define OFF_MAGIC               0x00U
#define OFF_VERSION             0x04U
#define OFF_SEQ                 0x08U
#define OFF_COMMAND             0x0cU
#define OFF_LENGTH              0x10U
#define OFF_STATUS              0x14U
#define OFF_FLAG                0x18U
#define OFF_ACK_LENGTH          0x1cU
#define PAYLOAD_OFFSET          64U
#define PAYLOAD_SIZE            3072U
#define ACK_OFFSET              (PAYLOAD_OFFSET + PAYLOAD_SIZE)
#define ACK_SIZE                512U

struct mailbox_request_header {
	__u32 command;
	__u32 length;
};

static unsigned long mailbox_phys = 0xb1001000UL;
module_param(mailbox_phys, ulong, 0444);
MODULE_PARM_DESC(mailbox_phys, "Jailhouse ROOTSHARED mailbox physical address");

static unsigned int timeout_ms = 1500;
module_param(timeout_ms, uint, 0644);
MODULE_PARM_DESC(timeout_ms, "ACK timeout in milliseconds");

static void __iomem *mailbox;
static DEFINE_MUTEX(mailbox_lock);
static __u32 sequence;
static char last_ack[ACK_SIZE];
static size_t last_ack_length;

static inline void mailbox_write32(unsigned int offset, __u32 value)
{
	writel(value, mailbox + offset);
}

static inline __u32 mailbox_read32(unsigned int offset)
{
	return readl(mailbox + offset);
}

static ssize_t mailbox_write(struct file *file, const char __user *buffer,
			     size_t count, loff_t *position)
{
	struct mailbox_request_header header;
	char payload[PAYLOAD_SIZE];
	unsigned long deadline;
	__u32 ack_length;

	if (count < sizeof(header))
		return -EINVAL;
	if (copy_from_user(&header, buffer, sizeof(header)))
		return -EFAULT;
	if (header.length > PAYLOAD_SIZE || count != sizeof(header) + header.length)
		return -EMSGSIZE;
	if (header.length && copy_from_user(payload, buffer + sizeof(header), header.length))
		return -EFAULT;

	if (mutex_lock_interruptible(&mailbox_lock))
		return -ERESTARTSYS;

	sequence++;
	if (!sequence)
		sequence++;
	last_ack_length = 0;
	memset(last_ack, 0, sizeof(last_ack));

	if (header.length)
		memcpy_toio(mailbox + PAYLOAD_OFFSET, payload, header.length);
	mailbox_write32(OFF_MAGIC, MAILBOX_MAGIC);
	mailbox_write32(OFF_VERSION, MAILBOX_VERSION);
	mailbox_write32(OFF_SEQ, sequence);
	mailbox_write32(OFF_COMMAND, header.command);
	mailbox_write32(OFF_LENGTH, header.length);
	mailbox_write32(OFF_STATUS, 0);
	wmb();
	mailbox_write32(OFF_FLAG, MAILBOX_FLAG_REQUEST);

	deadline = jiffies + msecs_to_jiffies(timeout_ms);
	while (time_before(jiffies, deadline)) {
		rmb();
		if (mailbox_read32(OFF_FLAG) == MAILBOX_FLAG_ACK &&
		    mailbox_read32(OFF_SEQ) == sequence)
			break;
		usleep_range(500, 1000);
	}
	if (mailbox_read32(OFF_FLAG) != MAILBOX_FLAG_ACK ||
	    mailbox_read32(OFF_SEQ) != sequence) {
		mutex_unlock(&mailbox_lock);
		return -ETIMEDOUT;
	}

	ack_length = min_t(__u32, mailbox_read32(OFF_ACK_LENGTH), ACK_SIZE - 1);
	if (ack_length) {
		memcpy_fromio(last_ack, mailbox + ACK_OFFSET, ack_length);
		last_ack[ack_length] = '\0';
		last_ack_length = ack_length;
	}
	mutex_unlock(&mailbox_lock);
	return count;
}

static ssize_t mailbox_read(struct file *file, char __user *buffer,
			    size_t count, loff_t *position)
{
	ssize_t result;

	if (mutex_lock_interruptible(&mailbox_lock))
		return -ERESTARTSYS;
	result = simple_read_from_buffer(buffer, count, position,
					 last_ack, last_ack_length);
	mutex_unlock(&mailbox_lock);
	return result;
}

static const struct file_operations mailbox_fops = {
	.owner = THIS_MODULE,
	.read = mailbox_read,
	.write = mailbox_write,
	.llseek = no_llseek,
};

static struct miscdevice mailbox_device = {
	.minor = MISC_DYNAMIC_MINOR,
	.name = DRIVER_NAME,
	.fops = &mailbox_fops,
	.mode = 0660,
};

static int __init mailbox_init(void)
{
	int ret;

	mailbox = ioremap(mailbox_phys, 0x1000);
	if (!mailbox)
		return -ENOMEM;
	ret = misc_register(&mailbox_device);
	if (ret) {
		iounmap(mailbox);
		return ret;
	}
	pr_info(DRIVER_NAME ": mapped mailbox at 0x%lx\n", mailbox_phys);
	return 0;
}

static void __exit mailbox_exit(void)
{
	misc_deregister(&mailbox_device);
	iounmap(mailbox);
}

module_init(mailbox_init);
module_exit(mailbox_exit);

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Smart Home Project");
MODULE_DESCRIPTION("Jailhouse shared-memory mailbox bridge for smart-home commands");
