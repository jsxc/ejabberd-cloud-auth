import logging
import traceback
import unicodedata
import sys
import time
from xclib.ejabberdctl import ejabberdctl
from xclib.utf8 import utf8, unutf8, utf8l

def sanitize(name):
    name = str(name)
    printable = {'Lu', 'Ll', 'Lm', 'Lo', 'Nd', 'Nl', 'No', 'Pc', 'Pd', 'Ps', 'Pe', 'Pi', 'Pf', 'Po', 'Sm', 'Sc', 'Sk', 'So', 'Zs'}
    return ''.join(c for c in name if unicodedata.category(c) in printable and c != '@')

class roster_thread:
    def roster_background_thread(self, sr):
        '''Entry for background roster update thread'''
        start = time.time()
        ucommands = gcommands = ()
        try:
            logging.debug('roster_thread for ' + str(sr))
            # Allow test hooks with static ejabberd_controller
            if hasattr(self.ctx, 'ejabberd_controller') and self.ctx.ejabberd_controller is not None:
                e = self.ctx.ejabberd_controller
            else:
                e = ejabberdctl(self.ctx)
            groups, ucommands = self.roster_update_users(e, sr)
            self.ctx.db.conn.dump('rosterinfo')
            gcommands = self.roster_update_groups(e, groups)
        except Exception as err:
            (etype, value, tb) = sys.exc_info()
            traceback.print_exception(etype, value, tb)
            logging.warn('roster_groups thread: %s:\n%s'
                         % (str(err), ''.join(traceback.format_tb(tb))))
            logging.debug('roster_groups thread failed after %s+%s commands in %s seconds',
                    len(ucommands), len(gcommands), time.time() - start)
            return False
        finally:
            logging.debug('roster_groups thread finished %s+%s commands in %s seconds',
                    len(ucommands), len(gcommands), time.time() - start)
        
    def roster_update_users(self, e, sr):
        '''Update users' full names and invert hash

For all *users* we have information about:
- collect the shared roster groups they belong to
- set their full names if not yet defined
Return inverted hash'''
        groups = {}
        commands = []
        if len(sr) == 0:
            # Empty roster information arrives as [] instead of {},
            # so sr.items() below would fail.
            return groups, commands
        for user, desc in sr.items():
            logging.debug('roster_update_users: user=%s, desc=%s' % (user, desc))
            if 'groups' in desc:
                for g in desc['groups']:
                    # Ignore groups ending in U+200B Zero-Width Space
                    if g.endswith('\u200b'):
                        logging.info('Ignoring group %s (ends with U+200B)', g)
                        continue
                    if g in groups:
                        groups[g].append(user)
                    else:
                        groups[g] = [user]
            if 'name' in desc:
                logging.debug('name in desc')
                lhs, rhs = self.jidsplit(user)
                jid = '@'.join((lhs, rhs))
                cached_name = None
                for row in self.ctx.db.conn.execute(
                        'SELECT fullname FROM rosterinfo WHERE jid=?',
                        (jid,)):
                    cached_name = row['fullname']
                logging.debug('cached_name = %s' % (cached_name,))
                if cached_name != desc['name']:
                    self.ctx.db.conn.begin()
                    self.ctx.db.conn.execute(
                            '''INSERT OR IGNORE INTO rosterinfo (jid)
                            VALUES (?)''', (jid,))
                    self.ctx.db.conn.execute(
                            '''UPDATE rosterinfo
                            SET fullname = ?
                            WHERE jid = ?''', (desc['name'], jid))
                    self.ctx.db.conn.commit()
                    logging.debug('set_vcard')
                    e.execute(['set_vcard', lhs, rhs, 'FN', desc['name']])
                    commands.append(('set_vcard', jid, desc['name']))
        return groups, commands

    def roster_update_groups(self, e, groups):
        '''Update shared roster groups with ejabberdctl

For all the *groups* we have information about:
- create the group (idempotent)
- delete the users that we do not know about anymore (idempotent)
- add the users we know about (idempotent)'''
        commands = []
        cleanname = {}
        loginjid = '@'.join((self.username, self.domain))
        logingroups = []
        for g in groups:
            cleanname[g] = sanitize(g)
            key = '@'.join((cleanname[g], self.domain))
            logging.debug('roster_update_groups: %s', key)
            previous_users = ()
            for row in self.ctx.db.conn.execute(
                    '''SELECT userlist FROM rostergroups
                    WHERE groupname=?''', (key,)):
                previous_users = row['userlist'].split('\t')
            logging.debug('previous_users=%s', previous_users)
            if previous_users == ():
                e.execute(['srg_create', cleanname[g], self.domain, cleanname[g], cleanname[g], cleanname[g]])
                commands.append(('srg_create', cleanname[g], self.domain))
                # Fill cache (again)
                previous_users = e.members(cleanname[g], self.domain)
            logging.debug('previous_users2=%s', previous_users)
            current_users = {}
            for u in groups[g]:
                if u == loginjid:
                    logingroups.append(cleanname[g])
                (lhs, rhs) = self.jidsplit(u)
                fulljid = '%s@%s' % (lhs, rhs)
                current_users[fulljid] = True
                logging.debug('current_users ' + ' '.join(sorted(current_users.keys())))
                if not fulljid in previous_users:
                    e.execute(['srg_user_add', lhs, rhs, cleanname[g], self.domain])
                    commands.append(('srg_user_add', fulljid, cleanname[g]))
            for p in previous_users:
                (lhs, rhs) = self.jidsplit(p)
                if p not in current_users:
                    e.execute(['srg_user_del', lhs, rhs, cleanname[g], self.domain])
                    commands.append(('srg_user_del', p, cleanname[g]))
            # Here, we could use INSERT OR REPLACE, because we fill
            # all the fields. But only until someone would add
            # extra fields, which then would be reset to default values.
            # Better safe than sorry.
            self.ctx.db.conn.begin()
            self.ctx.db.conn.execute(
                    '''INSERT OR IGNORE INTO rostergroups (groupname)
                    VALUES (?)''', (key,))
            self.ctx.db.conn.execute(
                    '''UPDATE rostergroups
                    SET userlist = ?
                    WHERE groupname = ?''', ('\t'.join(sorted(current_users.keys())), key))
            logging.debug('groupname %s, userlist %s', key, ('\t'.join(sorted(current_users.keys()))))
            self.ctx.db.conn.commit()
            self.ctx.db.conn.dump('rosterinfo')

        # For all the groups the login user was previously a member of:
        # - delete her from the shared roster group if no longer a member
        key = '@'.join((self.username, self.domain))
        previous = ()
        for row in self.ctx.db.conn.execute(
                '''SELECT grouplist FROM rosterinfo WHERE jid=?''', (key,)):
            if row['grouplist']: # Not None or ''
                previous = row['grouplist'].split('\t')
        logging.debug('previous %s group list %s', key, previous)
        logging.debug('cleannames %s', cleanname.values())
        logging.debug('logingroups %s', logingroups)
        for p in previous:
            if p not in logingroups:
                e.execute(['srg_user_del', self.username, self.domain, p, self.domain])
                commands.append(('srg_user_del2', key, p))
        # Only update when necessary
        new = '\t'.join(sorted(logingroups))
        logging.debug('new %s group list %s', key, new)
        if previous != new:
            self.ctx.db.conn.begin()
            self.ctx.db.conn.execute(
                    '''INSERT OR IGNORE INTO rosterinfo (jid)
                    VALUES (?)''', (key,))
            self.ctx.db.conn.execute(
                    '''UPDATE rosterinfo
                    SET grouplist = ?
                    WHERE jid = ?''', (new, key))
            self.ctx.db.conn.commit()
            logging.debug('jid %s, set grouplist %s', key, new)
        return commands
